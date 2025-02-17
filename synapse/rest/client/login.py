# Copyright 2014-2021 The Matrix.org Foundation C.I.C.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import logging
import random
import re
import string
import time
from re import match

from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
    Union,
)
from urllib import parse

import eospy.api
import eospy.http_client
import requests

from typing_extensions import TypedDict

from synapse.api.constants import ApprovalNoticeMedium
from synapse.api.errors import (
    Codes,
    InvalidClientTokenError,
    LoginError,
    NotApprovedError,
    SynapseError,
)
from synapse.api.ratelimiting import Ratelimiter
from synapse.api.urls import CLIENT_API_PREFIX
from synapse.appservice import ApplicationService
from synapse.handlers.auth import convert_client_dict_legacy_fields_to_identifier
from synapse.handlers.sso import SsoIdentityProvider
from synapse.http import get_request_uri
from synapse.http.server import HttpServer, finish_request
from synapse.http.servlet import (
    RestServlet,
    assert_params_in_dict,
    parse_bytes_from_args,
    parse_json_object_from_request,
    parse_string,
)
from synapse.http.site import SynapseRequest
from synapse.rest.client._base import client_patterns
from synapse.rest.well_known import WellKnownBuilder
from synapse.types import JsonDict, UserID

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class LoginResponse(TypedDict, total=False):
    user_id: str
    access_token: Optional[str]
    home_server: str
    expires_in_ms: Optional[int]
    refresh_token: Optional[str]
    device_id: Optional[str]
    well_known: Optional[Dict[str, Any]]


class LoginRestServlet(RestServlet):
    PATTERNS = client_patterns("/login$", v1=True)
    CAS_TYPE = "m.login.cas"
    SSO_TYPE = "m.login.sso"
    TOKEN_TYPE = "m.login.token"
    JWT_TYPE = "org.matrix.login.jwt"
    APPSERVICE_TYPE = "m.login.application_service"
    REFRESH_TOKEN_PARAM = "refresh_token"
    SIGNATURE_TYPE = "m.login.signature"
    AMAX_SIGNATURE_TYPE = "m.login.amaxsignature"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.hs = hs

        # JWT configuration variables.
        self.jwt_enabled = hs.config.jwt.jwt_enabled
        self.jwt_secret = hs.config.jwt.jwt_secret
        self.jwt_subject_claim = hs.config.jwt.jwt_subject_claim
        self.jwt_algorithm = hs.config.jwt.jwt_algorithm
        self.jwt_issuer = hs.config.jwt.jwt_issuer
        self.jwt_audiences = hs.config.jwt.jwt_audiences

        # SSO configuration.
        self.saml2_enabled = hs.config.saml2.saml2_enabled
        self.cas_enabled = hs.config.cas.cas_enabled
        self.oidc_enabled = hs.config.oidc.oidc_enabled
        self._refresh_tokens_enabled = (
            hs.config.registration.refreshable_access_token_lifetime is not None
        )

        # Whether we need to check if the user has been approved or not.
        self._require_approval = (
            hs.config.experimental.msc3866.enabled
            and hs.config.experimental.msc3866.require_approval_for_new_accounts
        )

        self.auth = hs.get_auth()

        self.clock = hs.get_clock()

        self.auth_handler = self.hs.get_auth_handler()
        self.registration_handler = hs.get_registration_handler()
        self._sso_handler = hs.get_sso_handler()

        self._well_known_builder = WellKnownBuilder(hs)
        self._address_ratelimiter = Ratelimiter(
            store=hs.get_datastores().main,
            clock=hs.get_clock(),
            rate_hz=self.hs.config.ratelimiting.rc_login_address.per_second,
            burst_count=self.hs.config.ratelimiting.rc_login_address.burst_count,
        )
        self._account_ratelimiter = Ratelimiter(
            store=hs.get_datastores().main,
            clock=hs.get_clock(),
            rate_hz=self.hs.config.ratelimiting.rc_login_account.per_second,
            burst_count=self.hs.config.ratelimiting.rc_login_account.burst_count,
        )

        # self.client = eospy.api.ChainApi(['http://139.224.250.244:18888/'], 10)
        self.amax_rpc = hs.config.server.amax_rpc_url
        self.amax_client = eospy.api.ChainApi([self.amax_rpc], 10)
        # redis
        self._external_cache = hs.get_external_cache()
        self._cache_name = 'cache_sign_msg'

        # 将验签服务的URL和chain_id提取为环境变量
        self.amax_chain_id = hs.config.login.chain_id
        self.amax_signature_url = hs.config.login.signature_url

        # ensure the CAS/SAML/OIDC handlers are loaded on this worker instance.
        # The reason for this is to ensure that the auth_provider_ids are registered
        # with SsoHandler, which in turn ensures that the login/registration prometheus
        # counters are initialised for the auth_provider_ids.
        _load_sso_handlers(hs)

    def on_GET(self, request: SynapseRequest) -> Tuple[int, JsonDict]:
        flows: List[JsonDict] = []
        if self.jwt_enabled:
            flows.append({"type": LoginRestServlet.JWT_TYPE})

        if self.cas_enabled:
            # we advertise CAS for backwards compat, though MSC1721 renamed it
            # to SSO.
            flows.append({"type": LoginRestServlet.CAS_TYPE})

        if self.cas_enabled or self.saml2_enabled or self.oidc_enabled:
            flows.append(
                {
                    "type": LoginRestServlet.SSO_TYPE,
                    "identity_providers": [
                        _get_auth_flow_dict_for_idp(idp)
                        for idp in self._sso_handler.get_identity_providers().values()
                    ],
                }
            )

            # While it's valid for us to advertise this login type generally,
            # synapse currently only gives out these tokens as part of the
            # SSO login flow.
            # Generally we don't want to advertise login flows that clients
            # don't know how to implement, since they (currently) will always
            # fall back to the fallback API if they don't understand one of the
            # login flow types returned.
            flows.append({"type": LoginRestServlet.TOKEN_TYPE})

        flows.extend({"type": t} for t in self.auth_handler.get_supported_login_types())

        flows.append({"type": LoginRestServlet.APPSERVICE_TYPE})

        return 200, {"flows": flows}

    async def on_POST(self, request: SynapseRequest) -> Tuple[int, LoginResponse]:
        login_submission = parse_json_object_from_request(request)

        # Check to see if the client requested a refresh token.
        client_requested_refresh_token = login_submission.get(
            LoginRestServlet.REFRESH_TOKEN_PARAM, False
        )
        if not isinstance(client_requested_refresh_token, bool):
            raise SynapseError(400, "`refresh_token` should be true or false.")

        should_issue_refresh_token = (
            self._refresh_tokens_enabled and client_requested_refresh_token
        )

        try:
            if login_submission["type"] == LoginRestServlet.APPSERVICE_TYPE:
                requester = await self.auth.get_user_by_req(request)
                appservice = requester.app_service

                if appservice is None:
                    raise InvalidClientTokenError(
                        "This login method is only valid for application services"
                    )

                if appservice.is_rate_limited():
                    await self._address_ratelimiter.ratelimit(
                        None, request.getClientAddress().host
                    )

                result = await self._do_appservice_login(
                    login_submission,
                    appservice,
                    should_issue_refresh_token=should_issue_refresh_token,
                )
            elif (
                self.jwt_enabled
                and login_submission["type"] == LoginRestServlet.JWT_TYPE
            ):
                await self._address_ratelimiter.ratelimit(
                    None, request.getClientAddress().host
                )
                result = await self._do_jwt_login(
                    login_submission,
                    should_issue_refresh_token=should_issue_refresh_token,
                )
            elif login_submission["type"] == LoginRestServlet.TOKEN_TYPE:
                await self._address_ratelimiter.ratelimit(
                    None, request.getClientAddress().host
                )
                result = await self._do_token_login(
                    login_submission,
                    should_issue_refresh_token=should_issue_refresh_token,
                )

            elif login_submission["type"] == LoginRestServlet.SIGNATURE_TYPE:
                await self._address_ratelimiter.ratelimit(
                    None, request.getClientAddress().host
                )
                result = await self._do_signature_login(
                    login_submission,
                    should_issue_refresh_token=should_issue_refresh_token,
                )
            elif login_submission["type"] == LoginRestServlet.AMAX_SIGNATURE_TYPE:
                await self._address_ratelimiter.ratelimit(
                    None, request.getClientAddress().host
                )
                result = await self._do_amax_signature_login(
                    login_submission,
                    should_issue_refresh_token=should_issue_refresh_token,
                )
            else:
                await self._address_ratelimiter.ratelimit(
                    None, request.getClientAddress().host
                )
                result = await self._do_other_login(
                    login_submission,
                    should_issue_refresh_token=should_issue_refresh_token,
                )
        except KeyError:
            raise SynapseError(400, "Missing JSON keys.")

        if self._require_approval:
            approved = await self.auth_handler.is_user_approved(result["user_id"])
            if not approved:
                raise NotApprovedError(
                    msg="This account is pending approval by a server administrator.",
                    approval_notice_medium=ApprovalNoticeMedium.NONE,
                )

        well_known_data = self._well_known_builder.get_well_known()
        if well_known_data:
            result["well_known"] = well_known_data
        return 200, result

    async def _do_appservice_login(
        self,
        login_submission: JsonDict,
        appservice: ApplicationService,
        should_issue_refresh_token: bool = False,
    ) -> LoginResponse:
        identifier = login_submission.get("identifier")
        logger.info("Got appservice login request with identifier: %r", identifier)

        if not isinstance(identifier, dict):
            raise SynapseError(
                400, "Invalid identifier in login submission", Codes.INVALID_PARAM
            )

        # this login flow only supports identifiers of type "m.id.user".
        if identifier.get("type") != "m.id.user":
            raise SynapseError(
                400, "Unknown login identifier type", Codes.INVALID_PARAM
            )

        user = identifier.get("user")
        if not isinstance(user, str):
            raise SynapseError(400, "Invalid user in identifier", Codes.INVALID_PARAM)

        if user.startswith("@"):
            qualified_user_id = user
        else:
            qualified_user_id = UserID(user, self.hs.hostname).to_string()

        if not appservice.is_interested_in_user(qualified_user_id):
            raise LoginError(403, "Invalid access_token", errcode=Codes.FORBIDDEN)

        return await self._complete_login(
            qualified_user_id,
            login_submission,
            ratelimit=appservice.is_rate_limited(),
            should_issue_refresh_token=should_issue_refresh_token,
        )

    async def _do_other_login(
        self, login_submission: JsonDict, should_issue_refresh_token: bool = False
    ) -> LoginResponse:
        """Handle non-token/saml/jwt logins

        Args:
            login_submission:
            should_issue_refresh_token: True if this login should issue
                a refresh token alongside the access token.

        Returns:
            HTTP response
        """
        # Log the request we got, but only certain fields to minimise the chance of
        # logging someone's password (even if they accidentally put it in the wrong
        # field)
        logger.info(
            "Got login request with identifier: %r, medium: %r, address: %r, user: %r",
            login_submission.get("identifier"),
            login_submission.get("medium"),
            login_submission.get("address"),
            login_submission.get("user"),
        )
        canonical_user_id, callback = await self.auth_handler.validate_login(
            login_submission, ratelimit=True
        )
        result = await self._complete_login(
            canonical_user_id,
            login_submission,
            callback,
            should_issue_refresh_token=should_issue_refresh_token,
        )
        return result

    async def _do_amax_signature_login(
        self, login_submission: JsonDict, should_issue_refresh_token: bool = False
    ) -> LoginResponse:
        """新增AMAX登录方法, 不使用message进行验签, 直接使用验签服务进行验签.

        :param login_submission:
        :param should_issue_refresh_token:
        :return:
        """
        logger.info(
            "Got wallet login amax request with signature: %r, wallet_address: %r, authority: %r, scope: %r, "
            "expiration: %r",
            login_submission.get("signature"),
            login_submission.get("wallet_address"),
            login_submission.get("authority"),
            login_submission.get("scope"),
            login_submission.get("expiration"),
        )

        # 不用message进行验签.
        headers = {'Content-Type': 'application/x-www-form-urlencoded;charset=utf-8'}
        form_data = {
            "signature": login_submission.get("signature"),
            "wallet_address": login_submission.get("wallet_address"),
            "authority": login_submission.get("authority"),
            "scope": login_submission.get("scope"),
            "chain_id": self.amax_chain_id,
            "expiration": login_submission.get("expiration")
        }
        # 字典转换k1=v1 & k2=v2 模式
        data = parse.urlencode(form_data)
        req = requests.post(self.amax_signature_url, data=data, headers=headers)
        req_content = eval(req.content)
        print("signature_verify request url: %s, chain_id: %s, "
              "status code:%s, result:%s" % (self.amax_signature_url, self.amax_chain_id, req.status_code, req_content))
        if req_content['code'] != 200:
            raise SynapseError(400, "Unknown signature %s" % login_submission.get("signature"), Codes.INVALID_WALLET_SIGNATURE)
        username = login_submission.get("wallet_address")
        if username.startswith("@"):
            canonical_user_id = username
        else:
            canonical_user_id = UserID(username, self.hs.hostname).to_string()

        # 调用chain.get_accountn方法拿用户信息
        account = self.amax_client.get_account(username)
        if account is None:
            raise SynapseError(400, "Get user account failed", Codes.GET_CHAIN_ACCOUNT_FAILED)
        permissions = account.get('permissions')
        print("permissions==>", permissions)
        perm_name = permissions[0]['perm_name']
        print("perm_name==>", perm_name)

        ## TODO  active是第一个public_key, owner是第二个public_key. 用哪一个.
        user_public_key = permissions[0]['required_auth']['keys'][0]['key']
        canonical_uid = await self.auth_handler.check_user_exists(canonical_user_id)
        if not canonical_uid:
            print("register canonical_uid==>", canonical_uid)
            await self.registration_handler.register_user(
                localpart=UserID.from_string(canonical_user_id).localpart,
                password_hash=user_public_key
            )

        result = await self._complete_login(
            canonical_user_id,
            login_submission,
            callback=None,
            should_issue_refresh_token=should_issue_refresh_token,
        )
        return result

    async def _do_signature_login(
        self, login_submission: JsonDict, should_issue_refresh_token: bool = False
    ) -> LoginResponse:
        """Handle blockchain signature login

        Args:
            login_submission:
            should_issue_refresh_token: True if this login should issue
                a refresh token alongside the access token.

        Returns:
            HTTP response
        """
        # Log the request we got, but only certain fields to minimise the chance of
        # logging someone's password (even if they accidentally put it in the wrong
        # field)
        logger.info(
            "Got wallet login request with identifier: %r, medium: %r, address: %r, user: %r, message: %r, sinature: "
            "%r, timestamp: %r",
            login_submission.get("identifier"),
            login_submission.get("medium"),
            login_submission.get("address"),
            login_submission.get("user"),
            login_submission.get("message"),
            login_submission.get("signature"),
            login_submission.get("timestamp"),
        )

        # 先验证签名的message是否在redis中，是否过期
        # message_ts = login_submission.get("timestamp")
        req_message = login_submission.get("message")
        if not isinstance(req_message, str):
            raise SynapseError(400, "Bad parameter: message", Codes.INVALID_PARAM)

        nonce_str = req_message.split(':')[1]
        cache_message = await self._external_cache.get(self._cache_name, nonce_str)
        if cache_message is None:
            logger.error("redis cache mesage not found, cache_name: %r, key: %r", self._cache_name, nonce_str)
            raise SynapseError(400, "Wallet sign message is invalid, Please login "
                                    "again.", Codes.INVALID_SIGNATUR_MESSAGE)

        if cache_message != req_message:
            logger.warning("request message is invalid, Please logi, cache_message: "
                           "%r, req_message: %r", cache_message, req_message)
            raise SynapseError(400, "Wallet sign message is invalid, Please login "
                                    "again.", Codes.INVALID_SIGNATUR_MESSAGE)

        # 根据user查询账户信息
        # map old-school login fields into new-school "identifier" fields.
        identifier_dict = convert_client_dict_legacy_fields_to_identifier(
            login_submission
        )

        username = identifier_dict.get("user")
        if not username:
            raise SynapseError(400, "User identifier is missing 'user' key", Codes.INVALID_PARAM)

        # user是amax链账号，验证下

        # 调chain.get_accountn方法拿用户信息
        account = self.amax_client.get_account(username)
        if account is None:
            raise SynapseError(400, "Get user accoount failed", Codes.GET_CHAIN_ACCOUNT_FAILED)
        permissions = account.get('permissions')
        print("permissions==>", permissions)
        perm_name = permissions[0]['perm_name']
        print("perm_name==>", perm_name)

        user_publickey = permissions[0]['required_auth']['keys'][0]['key']
        print("user_publickey==>", user_publickey)
        if not user_publickey:
            raise SynapseError(400, "Get user_publickey failed", Codes.INVALID_PARSED_PUBLICKKEY)

        # 验证是否是amax链的公钥
        matched = match('AM', user_publickey)
        if matched is None:
            raise SynapseError(400, "Parsed user_publickey is invalid", Codes.INVALID_PARSED_PUBLICKKEY)

        canonical_user_id, callback = await self.auth_handler.validate_signature_login(
            login_submission, user_publickey, ratelimit=True
        )

        print("canonical_user_id===>", canonical_user_id)

        canonical_uid = await self.auth_handler.check_user_exists(canonical_user_id)
        if not canonical_uid:
            await self.registration_handler.register_user(
                localpart=UserID.from_string(canonical_user_id).localpart,
                password_hash=user_publickey
            )

        result = await self._complete_login(
            canonical_user_id,
            login_submission,
            callback,
            should_issue_refresh_token=should_issue_refresh_token,
        )
        return result

    async def _complete_login(
        self,
        user_id: str,
        login_submission: JsonDict,
        callback: Optional[Callable[[LoginResponse], Awaitable[None]]] = None,
        create_non_existent_users: bool = False,
        ratelimit: bool = True,
        auth_provider_id: Optional[str] = None,
        should_issue_refresh_token: bool = False,
        auth_provider_session_id: Optional[str] = None,
    ) -> LoginResponse:
        """Called when we've successfully authed the user and now need to
        actually login them in (e.g. create devices). This gets called on
        all successful logins.

        Applies the ratelimiting for successful login attempts against an
        account.

        Args:
            user_id: ID of the user to register.
            login_submission: Dictionary of login information.
            callback: Callback function to run after login.
            create_non_existent_users: Whether to create the user if they don't
                exist. Defaults to False.
            ratelimit: Whether to ratelimit the login request.
            auth_provider_id: The SSO IdP the user used, if any.
            should_issue_refresh_token: True if this login should issue
                a refresh token alongside the access token.
            auth_provider_session_id: The session ID got during login from the SSO IdP.

        Returns:
            Dictionary of account information after successful login.
        """

        # Before we actually log them in we check if they've already logged in
        # too often. This happens here rather than before as we don't
        # necessarily know the user before now.
        if ratelimit:
            await self._account_ratelimiter.ratelimit(None, user_id.lower())

        if create_non_existent_users:
            canonical_uid = await self.auth_handler.check_user_exists(user_id)
            if not canonical_uid:
                canonical_uid = await self.registration_handler.register_user(
                    localpart=UserID.from_string(user_id).localpart
                )
            user_id = canonical_uid

        device_id = login_submission.get("device_id")

        # If device_id is present, check that device_id is not longer than a reasonable 512 characters
        if device_id and len(device_id) > 512:
            raise LoginError(
                400,
                "device_id cannot be longer than 512 characters.",
                errcode=Codes.INVALID_PARAM,
            )

        if self._require_approval:
            approved = await self.auth_handler.is_user_approved(user_id)
            if not approved:
                # If the user isn't approved (and needs to be) we won't allow them to
                # actually log in, so we don't want to create a device/access token.
                return LoginResponse(
                    user_id=user_id,
                    home_server=self.hs.hostname,
                )

        initial_display_name = login_submission.get("initial_device_display_name")
        (
            device_id,
            access_token,
            valid_until_ms,
            refresh_token,
        ) = await self.registration_handler.register_device(
            user_id,
            device_id,
            initial_display_name,
            auth_provider_id=auth_provider_id,
            should_issue_refresh_token=should_issue_refresh_token,
            auth_provider_session_id=auth_provider_session_id,
        )

        result = LoginResponse(
            user_id=user_id,
            access_token=access_token,
            home_server=self.hs.hostname,
            device_id=device_id,
        )

        if valid_until_ms is not None:
            expires_in_ms = valid_until_ms - self.clock.time_msec()
            result["expires_in_ms"] = expires_in_ms

        if refresh_token is not None:
            result["refresh_token"] = refresh_token

        if callback is not None:
            await callback(result)

        return result

    async def _do_token_login(
        self, login_submission: JsonDict, should_issue_refresh_token: bool = False
    ) -> LoginResponse:
        """
        Handle the final stage of SSO login.

        Args:
            login_submission: The JSON request body.
            should_issue_refresh_token: True if this login should issue
                a refresh token alongside the access token.

        Returns:
            The body of the JSON response.
        """
        token = login_submission["token"]
        res = await self.auth_handler.consume_login_token(token)

        return await self._complete_login(
            res.user_id,
            login_submission,
            self.auth_handler._sso_login_callback,
            auth_provider_id=res.auth_provider_id,
            should_issue_refresh_token=should_issue_refresh_token,
            auth_provider_session_id=res.auth_provider_session_id,
        )

    async def _do_jwt_login(
        self, login_submission: JsonDict, should_issue_refresh_token: bool = False
    ) -> LoginResponse:
        token = login_submission.get("token", None)
        if token is None:
            raise LoginError(
                403, "Token field for JWT is missing", errcode=Codes.FORBIDDEN
            )

        from authlib.jose import JsonWebToken, JWTClaims
        from authlib.jose.errors import BadSignatureError, InvalidClaimError, JoseError

        jwt = JsonWebToken([self.jwt_algorithm])
        claim_options = {}
        if self.jwt_issuer is not None:
            claim_options["iss"] = {"value": self.jwt_issuer, "essential": True}
        if self.jwt_audiences is not None:
            claim_options["aud"] = {"values": self.jwt_audiences, "essential": True}

        try:
            claims = jwt.decode(
                token,
                key=self.jwt_secret,
                claims_cls=JWTClaims,
                claims_options=claim_options,
            )
        except BadSignatureError:
            # We handle this case separately to provide a better error message
            raise LoginError(
                403,
                "JWT validation failed: Signature verification failed",
                errcode=Codes.FORBIDDEN,
            )
        except JoseError as e:
            # A JWT error occurred, return some info back to the client.
            raise LoginError(
                403,
                "JWT validation failed: %s" % (str(e),),
                errcode=Codes.FORBIDDEN,
            )

        try:
            claims.validate(leeway=120)  # allows 2 min of clock skew

            # Enforce the old behavior which is rolled out in productive
            # servers: if the JWT contains an 'aud' claim but none is
            # configured, the login attempt will fail
            if claims.get("aud") is not None:
                if self.jwt_audiences is None or len(self.jwt_audiences) == 0:
                    raise InvalidClaimError("aud")
        except JoseError as e:
            raise LoginError(
                403,
                "JWT validation failed: %s" % (str(e),),
                errcode=Codes.FORBIDDEN,
            )

        user = claims.get(self.jwt_subject_claim, None)
        if user is None:
            raise LoginError(403, "Invalid JWT", errcode=Codes.FORBIDDEN)

        user_id = UserID(user, self.hs.hostname).to_string()
        result = await self._complete_login(
            user_id,
            login_submission,
            create_non_existent_users=True,
            should_issue_refresh_token=should_issue_refresh_token,
        )
        return result


def _get_auth_flow_dict_for_idp(idp: SsoIdentityProvider) -> JsonDict:
    """Return an entry for the login flow dict

    Returns an entry suitable for inclusion in "identity_providers" in the
    response to GET /_matrix/client/r0/login

    Args:
        idp: the identity provider to describe
    """
    e: JsonDict = {"id": idp.idp_id, "name": idp.idp_name}
    if idp.idp_icon:
        e["icon"] = idp.idp_icon
    if idp.idp_brand:
        e["brand"] = idp.idp_brand
    return e


class RefreshTokenServlet(RestServlet):
    PATTERNS = client_patterns("/refresh$")

    def __init__(self, hs: "HomeServer"):
        self._auth_handler = hs.get_auth_handler()
        self._clock = hs.get_clock()
        self.refreshable_access_token_lifetime = (
            hs.config.registration.refreshable_access_token_lifetime
        )
        self.refresh_token_lifetime = hs.config.registration.refresh_token_lifetime

    async def on_POST(self, request: SynapseRequest) -> Tuple[int, JsonDict]:
        refresh_submission = parse_json_object_from_request(request)

        assert_params_in_dict(refresh_submission, ["refresh_token"])
        token = refresh_submission["refresh_token"]
        if not isinstance(token, str):
            raise SynapseError(400, "Invalid param: refresh_token", Codes.INVALID_PARAM)

        now = self._clock.time_msec()
        access_valid_until_ms = None
        if self.refreshable_access_token_lifetime is not None:
            access_valid_until_ms = now + self.refreshable_access_token_lifetime
        refresh_valid_until_ms = None
        if self.refresh_token_lifetime is not None:
            refresh_valid_until_ms = now + self.refresh_token_lifetime

        (
            access_token,
            refresh_token,
            actual_access_token_expiry,
        ) = await self._auth_handler.refresh_token(
            token, access_valid_until_ms, refresh_valid_until_ms
        )

        response: Dict[str, Union[str, int]] = {
            "access_token": access_token,
            "refresh_token": refresh_token,
        }

        # expires_in_ms is only present if the token expires
        if actual_access_token_expiry is not None:
            response["expires_in_ms"] = actual_access_token_expiry - now

        return 200, response


class SsoRedirectServlet(RestServlet):
    PATTERNS = list(client_patterns("/login/(cas|sso)/redirect$", v1=True)) + [
        re.compile(
            "^"
            + CLIENT_API_PREFIX
            + "/(r0|v3)/login/sso/redirect/(?P<idp_id>[A-Za-z0-9_.~-]+)$"
        )
    ]

    def __init__(self, hs: "HomeServer"):
        # make sure that the relevant handlers are instantiated, so that they
        # register themselves with the main SSOHandler.
        _load_sso_handlers(hs)
        self._sso_handler = hs.get_sso_handler()
        self._public_baseurl = hs.config.server.public_baseurl

    async def on_GET(
        self, request: SynapseRequest, idp_id: Optional[str] = None
    ) -> None:
        if not self._public_baseurl:
            raise SynapseError(400, "SSO requires a valid public_baseurl")

        # if this isn't the expected hostname, redirect to the right one, so that we
        # get our cookies back.
        requested_uri = get_request_uri(request)
        baseurl_bytes = self._public_baseurl.encode("utf-8")
        if not requested_uri.startswith(baseurl_bytes):
            # swap out the incorrect base URL for the right one.
            #
            # The idea here is to redirect from
            #    https://foo.bar/whatever/_matrix/...
            # to
            #    https://public.baseurl/_matrix/...
            #
            i = requested_uri.index(b"/_matrix")
            new_uri = baseurl_bytes[:-1] + requested_uri[i:]
            logger.info(
                "Requested URI %s is not canonical: redirecting to %s",
                requested_uri.decode("utf-8", errors="replace"),
                new_uri.decode("utf-8", errors="replace"),
            )
            request.redirect(new_uri)
            finish_request(request)
            return

        args: Dict[bytes, List[bytes]] = request.args  # type: ignore
        client_redirect_url = parse_bytes_from_args(args, "redirectUrl", required=True)
        sso_url = await self._sso_handler.handle_redirect_request(
            request,
            client_redirect_url,
            idp_id,
        )
        logger.info("Redirecting to %s", sso_url)
        request.redirect(sso_url)
        finish_request(request)


class CasTicketServlet(RestServlet):
    PATTERNS = client_patterns("/login/cas/ticket", v1=True)

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._cas_handler = hs.get_cas_handler()

    async def on_GET(self, request: SynapseRequest) -> None:
        client_redirect_url = parse_string(request, "redirectUrl")
        ticket = parse_string(request, "ticket", required=True)

        # Maybe get a session ID (if this ticket is from user interactive
        # authentication).
        session = parse_string(request, "session")

        # Either client_redirect_url or session must be provided.
        if not client_redirect_url and not session:
            message = "Missing string query parameter redirectUrl or session"
            raise SynapseError(400, message, errcode=Codes.MISSING_PARAM)

        await self._cas_handler.handle_ticket(
            request, ticket, client_redirect_url, session
        )

class RandomStrServlet(RestServlet):
    PATTERNS = client_patterns("/sign/get_random")

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.hs = hs

        # redis
        self._external_cache = hs.get_external_cache()
        self._cache_name = 'cache_sign_msg'
        self.wallet_sign_message = hs.config.server.wallet_sigin_message

    async def on_GET(self, request: SynapseRequest) -> Tuple[int, JsonDict]:
        rand_str = ''.join(random.sample(string.ascii_letters + string.digits, 32))

        # message = "Welcome to amax-synapse! sign nonce:{s}".format(s=rand_str)
        message = "{sign_ms} sign nonce:{s}".format(sign_ms=self.wallet_sign_message, s=rand_str)

        # message to redis, expiry_ms: 60000
        key = rand_str
        # test
        expiry_ms = 3000000
        await self._external_cache.set(self._cache_name, key, message, expiry_ms)

        response: Dict[str, Union[str, int]] = {
            "message": message,
            # "timestamp": key
        }

        return 200, response
        
def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    LoginRestServlet(hs).register(http_server)
    if hs.config.registration.refreshable_access_token_lifetime is not None:
        RefreshTokenServlet(hs).register(http_server)
    SsoRedirectServlet(hs).register(http_server)
    if hs.config.cas.cas_enabled:
        CasTicketServlet(hs).register(http_server)
    RandomStrServlet(hs).register(http_server)


def _load_sso_handlers(hs: "HomeServer") -> None:
    """Ensure that the SSO handlers are loaded, if they are enabled by configuration.

    This is mostly useful to ensure that the CAS/SAML/OIDC handlers register themselves
    with the main SsoHandler.

    It's safe to call this multiple times.
    """
    if hs.config.cas.cas_enabled:
        hs.get_cas_handler()
    if hs.config.saml2.saml2_enabled:
        hs.get_saml_handler()
    if hs.config.oidc.oidc_enabled:
        hs.get_oidc_handler()
