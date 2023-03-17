import datetime
import hashlib
import logging
import time
import urllib.parse
from typing import cast

import requests as requests

from saic_client_tosate.common_model import MessageV2, MessageBodyV2, Header
from saic_client_tosate.ota_v1_1.Message import MessageCoderV11
from saic_client_tosate.ota_v1_1.data_model import VinInfo, MpUserLoggingInReq, MpUserLoggingInRsp, AlarmSwitchReq, \
    MpAlarmSettingType, AlarmSwitch, MessageBodyV11, MessageV11, MessageListReq, StartEndNumber, MessageListResp, \
    Timestamp
from saic_client_tosate.ota_v2_1.Message import MessageCoderV21
from saic_client_tosate.ota_v2_1.data_model import OtaRvmVehicleStatusReq, OtaRvmVehicleStatusResp25857, OtaRvcReq,\
    RvcReqParam
from saic_client_tosate.ota_v3_0.Message import MessageCoderV30, MessageV30, MessageBodyV30
from saic_client_tosate.ota_v3_0.data_model import OtaChrgMangDataResp

UID_INIT = '0000000000000000000000000000000000000000000000000#'
logging.basicConfig(format='%(asctime)s %(message)s', level=logging.INFO)


class AbrpApi:
    def __init__(self, abrp_api_key: str, abrp_user_token: str) -> None:
        self.abrp_api_key = abrp_api_key
        self.abrp_user_token = abrp_user_token

    def update_abrp(self, vehicle_status: OtaRvmVehicleStatusResp25857, charge_status: OtaChrgMangDataResp):
        if (
                self.abrp_api_key is not None
                and self.abrp_user_token is not None
                and vehicle_status is not None
                and vehicle_status.get_gps_position() is not None
                and vehicle_status.get_gps_position().get_way_point() is not None
                and vehicle_status.get_gps_position().get_way_point().get_position() is not None
                and vehicle_status.get_gps_position().get_way_point().get_position().latitude > 0
                and vehicle_status.get_gps_position().get_way_point().get_position().longitude > 0
                and charge_status is not None
        ):
            # Request
            tlm_send_url = 'https://api.iternio.com/1/tlm/send'
            data = {
                'utc': vehicle_status.get_gps_position().timestamp_4_short.seconds,
                'soc': (charge_status.bmsPackSOCDsp / 10.0),
                'power': charge_status.get_power(),
                'speed': (vehicle_status.get_gps_position().get_way_point().speed / 10.0),
                'lat': (vehicle_status.get_gps_position().get_way_point().get_position().latitude / 1000000.0),
                'lon': (vehicle_status.get_gps_position().get_way_point().get_position().longitude / 1000000.0),
                'is_charging': vehicle_status.is_charging(),
                'is_parked': vehicle_status.is_parked(),
                'heading': vehicle_status.get_gps_position().get_way_point().heading,
                'elevation': vehicle_status.get_gps_position().get_way_point().get_position().altitude,
                'voltage': charge_status.get_voltage(),
                'current': charge_status.get_current()
            }
            exterior_temperature = vehicle_status.get_basic_vehicle_status().exterior_temperature
            if exterior_temperature != -128:
                data['ext_temp'] = exterior_temperature
            mileage = vehicle_status.get_basic_vehicle_status().mileage
            if mileage > 0:
                data['odometer'] = mileage / 10.0
            range_elec = vehicle_status.get_basic_vehicle_status().fuel_range_elec
            if range_elec > 0:
                data['est_battery_range'] = range_elec / 10.0

            tlm_response = requests.get(tlm_send_url, params={
                'api_key': self.abrp_api_key,
                'token': self.abrp_user_token,
                'tlm': urllib.parse.urlencode(data)
            })
            tlm_response.raise_for_status()
            print(f'ABRP: {tlm_response.content}')


class SaicApi:
    def __init__(self, saic_uri: str, saic_user: str, saic_password: str):
        self.saic_uri = saic_uri
        self.saic_user = saic_user
        self.saic_password = saic_password
        self.message_v1_1_coder = MessageCoderV11()
        self.message_V2_1_coder = MessageCoderV21()
        self.message_V3_0_coder = MessageCoderV30()
        self.cookies = None
        self.uid = ''
        self.token = ''
        self.token_expiration = None

    def login(self) -> MessageV11:
        application_data = MpUserLoggingInReq()
        application_data.password = self.saic_password
        header = Header()
        header.protocol_version = 17
        login_request_message = MessageV11(header, MessageBodyV11(), application_data)
        application_id = '501'
        application_data_protocol_version = 513
        self.message_v1_1_coder.initialize_message(
            UID_INIT[len(self.saic_user):] + self.saic_user,
            cast(str, None),
            application_id,
            application_data_protocol_version,
            1,
            login_request_message)
        self.publish_json_value(application_id, application_data_protocol_version, login_request_message.get_data())
        login_request_hex = self.message_v1_1_coder.encode_request(login_request_message)
        self.publish_raw_value(application_id, application_data_protocol_version, login_request_hex)
        login_response_hex = self.send_request(login_request_hex,
                                               urllib.parse.urljoin(self.saic_uri, '/TAP.Web/ota.mp'))
        self.publish_raw_value(application_id, application_data_protocol_version, login_response_hex)
        logging_in_rsp = MpUserLoggingInRsp()
        login_response_message = MessageV11(header, MessageBodyV11(), logging_in_rsp)
        self.message_v1_1_coder.decode_response(login_response_hex, login_response_message)
        self.publish_json_value(application_id, application_data_protocol_version, login_response_message.get_data())
        if login_response_message.body.error_message is not None:
            raise SystemExit(login_response_message.body.error_message.decode())
        else:
            self.uid = login_response_message.body.uid
            self.token = logging_in_rsp.token
            if logging_in_rsp.token_expiration is not None:
                self.token_expiration = logging_in_rsp.token_expiration
        return login_response_message

    def set_alarm_switches(self) -> None:
        alarm_switch_req = AlarmSwitchReq()
        for setting_type in MpAlarmSettingType:
            alarm_switch_req.alarm_switch_list.append(create_alarm_switch(setting_type))
        alarm_switch_req.pin = hash_md5('123456')

        header = Header()
        header.protocol_version = 17
        alarm_switch_req_message = MessageV11(header, MessageBodyV11(), alarm_switch_req)
        application_id = '521'
        application_data_protocol_version = 513
        self.message_v1_1_coder.initialize_message(
            self.uid,
            self.get_token(),
            application_id,
            application_data_protocol_version,
            1,
            alarm_switch_req_message)
        self.publish_json_value(application_id, application_data_protocol_version, alarm_switch_req_message.get_data())
        alarm_switch_request_hex = self.message_v1_1_coder.encode_request(alarm_switch_req_message)
        self.publish_raw_value(application_id, application_data_protocol_version, alarm_switch_request_hex)
        alarm_switch_response_hex = self.send_request(alarm_switch_request_hex,
                                                      urllib.parse.urljoin(self.saic_uri, '/TAP.Web/ota.mp'))
        self.publish_raw_value(application_id, application_data_protocol_version, alarm_switch_response_hex)
        alarm_switch_response_message = MessageV11(header, MessageBodyV11())
        self.message_v1_1_coder.decode_response(alarm_switch_response_hex, alarm_switch_response_message)
        self.publish_json_value(application_id, application_data_protocol_version,
                                alarm_switch_response_message.get_data())

        if alarm_switch_response_message.body.error_message is not None:
            raise ValueError(alarm_switch_response_message.body.error_message.decode())

    def get_vehicle_status(self, vin_info: VinInfo, event_id: str = None) -> MessageV2:
        vehicle_status_req = OtaRvmVehicleStatusReq()
        vehicle_status_req.veh_status_req_type = 2
        vehicle_status_req_msg = MessageV2(MessageBodyV2(), vehicle_status_req)
        application_id = '511'
        application_data_protocol_version = 25857
        self.message_V2_1_coder.initialize_message(self.uid, self.get_token(), vin_info.vin, application_id,
                                                   application_data_protocol_version, 1, vehicle_status_req_msg)
        if event_id is not None:
            vehicle_status_req_msg.body.event_id = event_id
        self.publish_json_value(application_id, application_data_protocol_version, vehicle_status_req_msg.get_data())
        vehicle_status_req_hex = self.message_V2_1_coder.encode_request(vehicle_status_req_msg)
        self.publish_raw_value(application_id, application_data_protocol_version, vehicle_status_req_hex)
        vehicle_status_rsp_hex = self.send_request(vehicle_status_req_hex,
                                                   urllib.parse.urljoin(self.saic_uri, '/TAP.Web/ota.mpv21'))
        self.publish_raw_value(application_id, application_data_protocol_version, vehicle_status_rsp_hex)
        vehicle_status_rsp_msg = MessageV2(MessageBodyV2(), OtaRvmVehicleStatusResp25857())
        self.message_V2_1_coder.decode_response(vehicle_status_rsp_hex, vehicle_status_rsp_msg)
        self.publish_json_value(application_id, application_data_protocol_version, vehicle_status_rsp_msg.get_data())
        return vehicle_status_rsp_msg

    def lock_vehicle(self, vin_info: VinInfo) -> None:
        rvc_params = []
        self.send_vehicle_ctrl_cmd_with_retry(vin_info, b'\x01', rvc_params)

    def unlock_vehicle(self, vin_info: VinInfo) -> None:
        rvc_params = []
        param1 = RvcReqParam()
        param1.param_id = 4
        param1.param_value = b'\x00'
        rvc_params.append(param1)

        param2 = RvcReqParam()
        param2.param_id = 5
        param2.param_value = b'\x00'
        rvc_params.append(param2)

        param3 = RvcReqParam()
        param3.param_id = 6
        param3.param_value = b'\x00'
        rvc_params.append(param3)

        param4 = RvcReqParam()
        param4.param_id = 7
        param4.param_value = b'\x03'
        rvc_params.append(param4)

        param5 = RvcReqParam()
        param5.param_id = 255
        param5.param_value = b'\x00'
        rvc_params.append(param5)

        self.send_vehicle_ctrl_cmd_with_retry(vin_info, b'\x02', rvc_params)

    def start_rear_window_heat(self, vin_info: VinInfo):
        rvc_params = []
        param1 = RvcReqParam()
        param1.param_id = 23
        param1.param_value = b'\x01'
        rvc_params.append(param1)

        param2 = RvcReqParam()
        param2.param_id = 255
        param2.param_value = b'\x00'
        rvc_params.append(param2)

        self.send_vehicle_ctrl_cmd_with_retry(vin_info, b'\x20', rvc_params)

    def stop_rear_window_heat(self, vin_info: VinInfo):
        rvc_params = []
        param1 = RvcReqParam()
        param1.param_id = 23
        param1.param_value = b'\x00'
        rvc_params.append(param1)

        param2 = RvcReqParam()
        param2.param_id = 255
        param2.param_value = b'\x00'
        rvc_params.append(param2)

        self.send_vehicle_ctrl_cmd_with_retry(vin_info, b'\x20', rvc_params)

    def send_vehicle_ctrl_cmd_with_retry(self, vin_info: VinInfo, rvc_req_type: bytes, rvc_params: list):
        vehicle_control_cmd_rsp_msg = self.send_vehicle_control_command(vin_info, rvc_req_type, rvc_params)
        retry = 1
        while (
                vehicle_control_cmd_rsp_msg.body.error_message is not None
                and retry < 3
        ):
            time.sleep(float(2))
            event_id = vehicle_control_cmd_rsp_msg.body.event_id
            vehicle_control_cmd_rsp_msg = self.send_vehicle_control_command(vin_info, rvc_req_type, rvc_params,
                                                                            event_id)
            retry += 1

    def send_vehicle_control_command(self, vin_info: VinInfo, rvc_req_type: bytes, rvc_params: list,
                                     event_id: str = None) -> MessageV2:
        vehicle_control_req = OtaRvcReq()
        vehicle_control_req.rvc_req_type = rvc_req_type
        for p in rvc_params:
            param = cast(RvcReqParam, p)
            vehicle_control_req.rvc_params.append(param)

        vehicle_control_cmd_req_msg = MessageV2(MessageBodyV2(), vehicle_control_req)
        application_id = '510'
        application_data_protocol_version = 25857
        self.message_V2_1_coder.initialize_message(self.uid, self.get_token(), vin_info.vin, application_id,
                                                   application_data_protocol_version, 1, vehicle_control_cmd_req_msg)
        if event_id is not None:
            vehicle_control_cmd_req_msg.body.event_id = event_id
        self.publish_json_value(application_id, application_data_protocol_version,
                                vehicle_control_cmd_req_msg.get_data())
        vehicle_control_cmd_req_msg_hex = self.message_V2_1_coder.encode_request(vehicle_control_cmd_req_msg)
        self.publish_raw_value(application_id, application_data_protocol_version, vehicle_control_cmd_req_msg_hex)
        vehicle_control_cmd_rsp_msg_hex = self.send_request(vehicle_control_cmd_req_msg_hex,
                                                            urllib.parse.urljoin(self.saic_uri, '/TAP.Web/ota.mpv21'))
        self.publish_raw_value(application_id, application_data_protocol_version, vehicle_control_cmd_rsp_msg_hex)
        vehicle_control_cmd_rsp_msg = MessageV2(MessageBodyV2())
        self.message_V2_1_coder.decode_response(vehicle_control_cmd_rsp_msg_hex, vehicle_control_cmd_rsp_msg)
        self.publish_json_value(application_id, application_data_protocol_version,
                                vehicle_control_cmd_rsp_msg.get_data())
        return vehicle_control_cmd_rsp_msg

    def get_charging_status(self, vin_info: VinInfo, event_id: str = None) -> MessageV30:
        chrg_mgmt_data_req_msg = MessageV30(MessageBodyV30())
        application_id = '516'
        application_data_protocol_version = 768
        self.message_V3_0_coder.initialize_message(self.uid, self.get_token(), vin_info.vin, application_id,
                                                   application_data_protocol_version, 5, chrg_mgmt_data_req_msg)
        if event_id is not None:
            chrg_mgmt_data_req_msg.body.event_id = event_id
        self.publish_json_value(application_id, application_data_protocol_version, chrg_mgmt_data_req_msg.get_data())
        chrg_mgmt_data_req_hex = self.message_V3_0_coder.encode_request(chrg_mgmt_data_req_msg)
        self.publish_raw_value(application_id, application_data_protocol_version, chrg_mgmt_data_req_hex)
        chrg_mgmt_data_rsp_hex = self.send_request(chrg_mgmt_data_req_hex,
                                                   urllib.parse.urljoin(self.saic_uri, '/TAP.Web/ota.mpv30'))
        self.publish_raw_value(application_id, application_data_protocol_version, chrg_mgmt_data_rsp_hex)
        chrg_mgmt_data_rsp_msg = MessageV30(MessageBodyV30(), OtaChrgMangDataResp())
        self.message_V3_0_coder.decode_response(chrg_mgmt_data_rsp_hex, chrg_mgmt_data_rsp_msg)
        self.publish_json_value(application_id, application_data_protocol_version, chrg_mgmt_data_rsp_msg.get_data())
        return chrg_mgmt_data_rsp_msg

    def get_message_list(self, event_id: str = None) -> MessageV11:
        message_list_request = MessageListReq()
        message_list_request.start_end_number = StartEndNumber()
        message_list_request.start_end_number.start_number = 1
        message_list_request.start_end_number.end_number = 5
        message_list_request.message_group = 'ALARM'

        header = Header()
        header.protocol_version = 18
        message_body = MessageBodyV11()
        message_list_req_msg = MessageV11(header, message_body, message_list_request)
        application_id = '531'
        application_data_protocol_version = 513
        self.message_v1_1_coder.initialize_message(self.uid, self.get_token(), application_id,
                                                   application_data_protocol_version, 1, message_list_req_msg)
        if event_id is not None:
            message_body.event_id = event_id
        message_list_req_msg.header.protocol_version = 18
        self.publish_json_value(application_id, application_data_protocol_version, message_list_req_msg.get_data())
        message_list_req_hex = self.message_v1_1_coder.encode_request(message_list_req_msg)
        self.publish_raw_value(application_id, application_data_protocol_version, message_list_req_hex)
        message_list_rsp_hex = self.send_request(message_list_req_hex,
                                                 urllib.parse.urljoin(self.saic_uri, '/TAP.Web/ota.mp'))
        self.publish_raw_value(application_id, application_data_protocol_version, message_list_rsp_hex)
        message_list_rsp_msg = MessageV11(header, MessageBodyV11(), MessageListResp())
        self.message_v1_1_coder.decode_response(message_list_rsp_hex, message_list_rsp_msg)
        self.publish_json_value(application_id, application_data_protocol_version, message_list_rsp_msg.get_data())
        return message_list_rsp_msg

    def publish_raw_value(self, application_id: str, application_data_protocol_version: int, raw: str):
        logging.debug(f'{application_id}_{application_data_protocol_version}/raw: {raw}')

    def publish_json_value(self, application_id: str, application_data_protocol_version: int, data: dict):
        logging.debug(f'{application_id}_{application_data_protocol_version}/json: {data}')

    def send_request(self, hex_message: str, endpoint) -> str:
        headers = {
            'Accept': '*/*',
            'Content-Type': 'text/html',
            'Accept-Encoding': 'gzip, deflate, br',
            'User-Agent': 'MG iSMART/1.1.1 (iPhone; iOS 16.3; Scale/3.00)',
            'Accept-Language': 'de-DE;q=1, en-DE;q=0.9, lu-DE;q=0.8, fr-DE;q=0.7',
            'Content-Length': str(len(hex_message))
        }

        response = requests.post(url=endpoint, data=hex_message, headers=headers, cookies=self.cookies)
        response.raise_for_status()
        self.cookies = response.cookies
        return response.content.decode()

    def get_token(self):
        if self.token_expiration is not None:
            token_expiration = cast(Timestamp, self.token_expiration)
            if token_expiration.get_timestamp() < datetime.datetime.now():
                self.login()
        return self.token


def hash_md5(password: str) -> str:
    return hashlib.md5(password.encode('utf-8')).hexdigest()


def create_alarm_switch(alarm_setting_type: MpAlarmSettingType) -> AlarmSwitch:
    alarm_switch = AlarmSwitch()
    alarm_switch.alarm_setting_type = alarm_setting_type.value
    alarm_switch.alarm_switch = True
    alarm_switch.function_switch = True
    return alarm_switch
