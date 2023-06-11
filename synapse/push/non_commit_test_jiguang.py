import base64
import json
from datetime import datetime
from typing import Dict, Any, List, Union

import requests


def getAuthoration(app_key, master_secret) -> str:
    origin_str = str.format('{0}:{1}', app_key, master_secret)
    origin_str = origin_str.encode('utf-8')
    base64_str_bytes = base64.b64encode(origin_str)
    print(base64_str_bytes)
    print(type(base64_str_bytes))
    base64_str = base64_str_bytes.decode()
    print(type(base64_str))
    print(base64_str)
    return base64_str


class HttpPushAction:
    """
    HttpPushAction instances include the information used to generate HTTP
    requests to a push gateway.
    """

    event_id: str
    room_id: str
    stream_ordering: int
    actions: List[Union[dict, str]]


def test_v3(HEADERS):
    now_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(now_time)
    FormData = {
        "platform": "android",
        "audience": {
            "registration_id": [
                "190e35f7e05797134a3"
            ]
        },
        "notification": {
            "android": {
                "title": "通知 title, James" + now_time,
                "alert": "通知内容, 测试离线消息, James" + now_time,
                # "builder_id": 0,
                "category": "alarm",
                # "small_icon": "mtpush_notification_icon",
                # "large_icon": "mtpush_notification_icon",
                "extras": {
                    "event_id": "$5xHzglxw3cjJfjrxvISC7TLDMneFIwcJd85lFKVn1X0",
                    "room_id": "!MRIhLkuiNUeHccjUOu:matrix.ambt.art",
                    "unread": 3,
                    "prio": "normal"
                },
                "priority": 1,
                "alert_type": 7,
                "sound": "coin",
                # "channel_id": "money",
                # "badge_add_num": 1,
                # "badge_class": "com.engagelab.app.activity.MainActivity",
                # "style": 2,
                # "big_text": "党的十八大提出，倡导富强、民主、文明、和谐，倡导自由、平等、公正、法治，倡导爱国、敬业、诚信、友善，积极培育和践行社会主义核心价值观。富强、民主、文明、和谐是国家层面的价值目标，自由、平等、公正、法治是社会层面的价值取向，爱国、敬业、诚信、友善是公民个人层面的价值准则，这 24 个字是社会主义核心价值观的基本内容。",
                # "inbox": {
                #     "inbox1": "this is inbox one",
                #     "inbox2": "this is inbox two",
                #     "inbox3": "this is inbox three"
                # },
                # "big_pic_path": "https://ss1.bdstatic.com/70cFuXSh_Q1YnxGkpoWK1HF6hhy/it/u=96071541,1913562332&fm=26&gp=0.jpg",
                # "intent": {
                #     "url": "intent:#Intent;component=com.engagelab.oaapp/com.engagelab.app.component.UserActivity400;end"
                # },
                "intent": {
                    "url": "intent:#Intent;component=com.aplink.wallet.dev/com.aplink.flutter_wallet_pptoken.MainActivity;end"
                }
            }
        },
        "options": {
            "third_party_channel": {
                "vivo": {
                    "classification": 1,
                    "pushMode": 1
                },
                "huawei": {
                    "distribution_new": "mtpush_pns",
                    "importance": "NORMAL",
                    "category": "IM"
                }
            }
        }
    }
    # FormData = {
    #     "platform": "android",
    #     "audience": {
    #         "registration_id": [
    #             "1a0018970ab4981bc47"
    #         ]
    #     },
    #     "message": {
    #         "content_type": "text",
    #         "title": "custom_title",
    #         "msg_content": "custom_content",
    #         "extras": {
    #             "key": "value"
    #         }
    #     }
    # }

    print(FormData)
    # req = requests.post("http://pricloud-master-api.glqas.mtpushoa.com/v3/push", data=json.dumps(FormData), headers=HEADERS)
    req = requests.post("https://push.api.engagelab.cc/v3/push", data=json.dumps(FormData),
                        headers=HEADERS)
    print(req.request.body)
    req_content = req.content
    print(req_content)
    print(req.status_code)


def test_v4(HEADERS):
    now_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(now_time)
    ## 自定义消息体
    FormData = {"request_id": "111", "to": {"registration_id": ["170976fa8a9275bd8f6"]},  ## 推送设备ID.
                "body": {
                    "platform": "all",
                    "message": {
                        "msg_content": "自定义消息" + now_time,
                        "content_type": "text",
                        "title": "James and Kobe" + now_time,
                        "extras": {
                            "event_id": "111",
                            "room_id": "11111",
                            "unread": 3,
                            "prio": "normal",
                            "type": 1 # type = 1表示社交(与前端沟通)
                        }
                    },
                    "options": {
                        "time_to_live": 86400,
                        "apns_production": False,
                        "third_party_channel": {
                            "huawei": {
                                "distribution_new": "mtpush_pns",
                                "importance": "NORMAL"
                                , "category": "IM"
                            }
                        }
                    },

                }
                }

    ## 通知消息体.
    # FormData = {"request_id": "111", "to": {"registration_id": ["170976fa8a9275bd8f6"]},  ## 推送设备ID.
    #             "body": {
    #                 "notification": {
    #                     "android": {
    #                         "alert": "通知消息" + now_time,
    #                         "title": "Push test" + now_time,
    #                         "sound": "sound.caf",
    #                         "extras": {
    #                             "event_id": "$BQu3DUudDMICIIX_tkIG9krw-x5fN75f25cGpMli3bw",
    #                             "room_id": "!WEKPSrerqihIGRoqmm:matrix.ambt.art",
    #                             "unread": 3,
    #                             "prio": "normal"
    #                         }
    #                     },
    #                     "ios": {
    #                         "alert": "hello, Push!" + now_time,
    #                         "sound": "sound.caf",
    #                         "badge": 1,
    #                         "extras": {
    #                             "event_id": "$BQu3DUudDMICIIX_tkIG9krw-x5fN75f25cGpMli3bw",
    #                             "room_id": "!WEKPSrerqihIGRoqmm:matrix.ambt.art",
    #                             "unread": 3,
    #                             "prio": "normal"
    #                         }
    #                     }
    #                 },
    #                 "platform": "all",
    #                 # "notification": {
    #                 #     "alert": "Hello, Push James and Kobe16 !"
    #                 # },
    #                 "options": {
    #                     "time_to_live": 86400,
    #                     "apns_production": False,
    #                     "third_party_channel": {
    #                         "huawei": {
    #                             "distribution_new": "mtpush_pns",
    #                             "importance": "NORMAL"
    #                             ,"category": "IM"
    #                         }
    #                     }
    #                 },
    #
    #             }
    #             }

    print(FormData)
    req = requests.post("https://push.api.engagelab.cc/v4/push", data=json.dumps(FormData), headers=HEADERS)
    print(req.request.body)
    req_content = req.content
    print(req_content)
    print(req.status_code)


# chen 这个问题是没有找到  "name 'msc3873 _escape_event_match_key' is not defined"，官网1.82 没有msc3873 _escape_event_match_key  取消了，我更新最新的官网文件， 现在还是 报错


if __name__ == '__main__':
    di = Dict[str, Any]
    di = {"a": "b"}
    print(str(di))

    http = HttpPushAction()
    http.room_id = "1"
    print(json.dumps(http.__dict__))

    appKey = '0ea1b2a7f651a4afa04af5fd'
    masterSecret = '24df3fd204bb0fa025b4e838'
    base64_str = getAuthoration(appKey, masterSecret)
    print(base64_str)

    HEADERS = {'Content-Type': 'application/json',
               'Authorization': 'Basic ' + base64_str}

    # test_v4(HEADERS)
    test_v3(HEADERS)

# if __name__ == '__main__':
#     print(isinstance(1, int))
#     print(type(1) is not int)
