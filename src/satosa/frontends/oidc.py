#!/usr/bin/env python
"""
A OpenID Connect frontend module for the satosa proxy
"""
import datetime
import json
import logging
from urllib.parse import urlencode

from oic.oic.message import AuthorizationResponse, AuthorizationRequest, AuthorizationErrorResponse, \
    RegistrationResponse, ProviderConfigurationResponse
from oic.oic.provider import Provider, RegistrationEndpoint, AuthorizationEndpoint
from oic.utils import shelve_wrapper
from oic.utils.http_util import SeeOther, Created, Response
from oic.utils.keyio import KeyBundle, KeyJar
from oic.utils.userinfo import UserInfo

from satosa.frontends.base import FrontendModule
from satosa.internal_data import InternalRequest
from satosa.logging_util import satosa_logging
from satosa.util import oidc_subject_type_to_hash_type

LOGGER = logging.getLogger(__name__)


class OIDCFrontend(FrontendModule):
    """
    A OpenID Connect frontend module
    """

    MANDATORY_CONFIG = {"issuer", "signing_key_path"}

    def __init__(self, auth_req_callback_func, internal_attributes, conf):
        self._validate_config(conf)
        super(OIDCFrontend, self).__init__(auth_req_callback_func, internal_attributes)

        self.state_id = type(self).__name__
        self.sign_alg = "RS256"
        self.subject_type_default = "pairwise"
        self.conf = conf

    def handle_authn_response(self, context, internal_resp):
        """
        See super class method satosa.frontends.base.FrontendModule#handle_authn_response
        :type context: satosa.context.Context
        :type internal_response: satosa.internal_data.InternalResponse
        :rtype oic.utils.http_util.Response
        """
        auth_req = self._get_authn_request_from_state(context.state)

        # filter attributes to return in ID Token as claims
        attributes = self.converter.from_internal("openid", internal_resp.get_attributes())
        satosa_logging(LOGGER, logging.DEBUG,
                       "Attributes delivered by backend to OIDC frontend: {}".format(
                               json.dumps(attributes)), context.state)
        flattened_attributes = {k: v[0] for k, v in attributes.items()}
        requested_id_token_claims = auth_req.get("claims", {}).get("id_token")
        user_claims = self._get_user_info(flattened_attributes,
                                          requested_id_token_claims,
                                          auth_req["scope"])
        satosa_logging(LOGGER, logging.DEBUG,
                       "Attributes filtered by requested claims/scope: {}".format(
                               json.dumps(user_claims)), context.state)

        # construct epoch timestamp of reported authentication time
        auth_time = datetime.datetime.strptime(internal_resp.auth_info.timestamp,
                                               "%Y-%m-%dT%H:%M:%SZ")
        epoch_timestamp = (auth_time - datetime.datetime(1970, 1, 1)).total_seconds()

        base_claims = {"client_id": auth_req["client_id"],
                       "sub": internal_resp.get_user_id(),
                       "nonce": auth_req["nonce"]}
        id_token = self.provider.id_token_as_signed_jwt(base_claims, user_info=user_claims,
                                                        auth_time=epoch_timestamp,
                                                        loa="",
                                                        alg=self.sign_alg)

        oidc_client_state = auth_req.get("state")
        kwargs = {}
        if oidc_client_state:  # inlcude any optional 'state' sent by the client in the authn req
            kwargs["state"] = oidc_client_state

        auth_resp = AuthorizationResponse(id_token=id_token, **kwargs)
        http_response = auth_resp.request(auth_req["redirect_uri"],
                                          self._should_fragment_encode(auth_req))
        return SeeOther(http_response)

    def handle_backend_error(self, exception):
        """
        See super class satosa.frontends.base.FrontendModule
        :type exception: satosa.exception.SATOSAError
        :rtype: oic.utils.http_util.Response
        """
        auth_req = self._get_authn_request_from_state(exception.state)
        error_resp = AuthorizationErrorResponse(error="access_denied",
                                                error_description=exception.message)
        satosa_logging(LOGGER, logging.DEBUG, exception.message, exception.state)
        return SeeOther(
                error_resp.request(auth_req["redirect_uri"],
                                   self._should_fragment_encode(auth_req)))

    def register_endpoints(self, providers):
        """
        See super class satosa.frontends.base.FrontendModule
        :type providers: list[str]
        :rtype: list[(str, ((satosa.context.Context, Any) -> satosa.response.Response, Any))]
        :raise ValueError: if more than one backend is configured
        """
        if len(providers) != 1:
            raise ValueError("OpenID Connect frontend only supports one backend.")
        backend = providers[0]

        endpoint_baseurl = "{}/{}".format(self.conf["issuer"], backend)
        jwks_uri = "{}/jwks".format(self.conf["issuer"])
        self._create_op(self.conf["issuer"], endpoint_baseurl, jwks_uri)

        provider_config = (
            "^.well-known/openid-configuration$", self._provider_config)
        jwks_uri = ("^jwks$", self._jwks)
        dynamic_client_registration = (
            "^{}/{}".format(backend, RegistrationEndpoint.url), self._register_client)
        authentication = (
            "^{}/{}".format(backend, AuthorizationEndpoint.url), self.handle_authn_request)

        url_map = [provider_config, jwks_uri, dynamic_client_registration, authentication]
        return url_map

    def _create_op(self, issuer, endpoint_baseurl, jwks_uri):
        """
        Create the necessary Provider instance.
        :type issuer: str
        :type endpoint_baseurl: str
        :type jwks_uri: str
        :param issuer: issuer URL for the OP
        :param endpoint_baseurl: baseurl to build endpoint URL from
        :param jwks_uri: URL to where the JWKS will be published
        """
        kj = KeyJar()
        signing_key = KeyBundle(source="file://{}".format(self.conf["signing_key_path"]),
                                fileformat="der", keyusage=["sig"])
        kj.add_kb("", signing_key)
        capabilities = {
            "response_types_supported": ["id_token"],
            "id_token_signing_alg_values_supported": [self.sign_alg],
            "response_modes_supported": ["fragment", "query"],
            "subject_types_supported": ["public", "pairwise"],
            "grant_types_supported": ["implicit"],
            "claim_types_supported": ["normal"],
            "claims_parameter_supported": True,
            "request_parameter_supported": False,
            "request_uri_parameter_supported": False,
        }

        if "client_db_path" in self.conf:
            cdb = shelve_wrapper.open(self.conf["client_db_path"])
        else:
            cdb = {}  # client db in memory only

        self.provider = Provider(issuer, None, cdb, None, None, None, None, None, keyjar=kj,
                                 capabilities=capabilities, jwks_uri=jwks_uri)
        self.provider.baseurl = endpoint_baseurl
        self.provider.endp = [RegistrationEndpoint, AuthorizationEndpoint]

    def _get_user_info(self, user_attributes, requested_claims=None, scopes=None):
        """
        Filter user attributes to return to the client  (as claims in the ID Token) based on what
        was requested in request 'claims' parameter and in the 'scope'.
        :type user_attributes: dict[str, str]
        :type requested_claims: dict[str, Optional[dict]]
        :type scopes: list[str]
        :rtype: dict[str, str]

        :param user_attributes: attributes provided by the backend
        :param requested_claims: claims requested by the client through the 'claims' request param
        :param scopes: the scopes requested by the client
        :return: all attributes/claims to return to the client
        """
        requested_claims = requested_claims or {}
        scopes = scopes or []
        claims_requested_by_scope = Provider._scope2claims(scopes)
        claims_requested_by_scope.update(
                requested_claims)  # let explicit claims request override scope

        return UserInfo().filter(user_attributes, claims_requested_by_scope)

    def _validate_config(self, config):
        """
        Validates that all necessary config parameters are specified.
        :type config: dict[str, dict[str, Any] | str]
        :param config: the module config
        """
        if config is None:
            raise ValueError("OIDCFrontend conf can't be 'None'.")

        for k in self.MANDATORY_CONFIG:
            if k not in config:
                raise ValueError(
                        "Missing configuration parameter '{}' for OpenID Connect frontend.".format(
                                k))

    def _should_fragment_encode(self, authn_req):
        """
        Determine, based on the clients request, whether the authentication/error response should
        be fragment encoded or not.
        :type authn_req: oic.oic.message.AuthorizationRequest
        :rtype: bool

        :param authn_req: parsed authentication request from the client
        :return: True if the response should be fragment encoded
        """
        return authn_req.get("response_mode", "fragment") == "fragment"

    def _get_authn_request_from_state(self, state):
        """
        Extract the clietns request stoed in the SATOSA state.
        :type state: satosa.state.State
        :rtype: oic.oic.message.AuthorizationRequest

        :param state: the current state
        :return: the parsed authentication request
        """
        stored_state = state.get(self.state_id)
        oidc_request = stored_state["oidc_request"]
        return AuthorizationRequest().deserialize(oidc_request)

    def _register_client(self, context):
        """
        Handle the OIDC dynamic client registration.
        :type context: satosa.context.Context
        :rtype: oic.utils.http_util.Response

        :param context: the current context
        :return: HTTP response to the client
        """
        http_resp = self.provider.registration_endpoint(json.dumps(context.request))
        if not isinstance(http_resp, Created):
            return http_resp

        return self._fixup_registration_response(http_resp)

    def _fixup_registration_response(self, http_resp):
        # remove client_secret since no token endpoint is published
        response = RegistrationResponse().deserialize(http_resp.message, "json")
        del response["client_secret"]
        # specify supported id token signing alg
        response["id_token_signed_response_alg"] = self.sign_alg

        http_resp.message = response.to_json()
        return http_resp

    def _provider_config(self, context):
        """
        Construct the provider configuration information (served at /.well-known/openid-configuration).
        :type context: satosa.context.Context
        :rtype: oic.utils.http_util.Response

        :param context: the current context
        :return: HTTP response to the client
        """
        http_resp = self.provider.providerinfo_endpoint()
        if not isinstance(http_resp, Response):
            return http_resp
        provider_config = ProviderConfigurationResponse().deserialize(http_resp.message, "json")
        del provider_config["token_endpoint_auth_methods_supported"]
        del provider_config["require_request_uri_registration"]

        http_resp.message = provider_config.to_json()
        return http_resp

    def handle_authn_request(self, context):
        """
        Parse and verify the authentication request and pass it on to the backend.
        :type context: satosa.context.Context
        :rtype: oic.utils.http_util.Response

        :param context: the current context
        :return: HTTP response to the client
        """

        # verify auth req (correct redirect_uri, contains nonce and response_type='id_token')
        request = urlencode(context.request)
        satosa_logging(LOGGER, logging.DEBUG, "Authn req from client: {}".format(request),
                       context.state)

        info = self.provider.auth_init(request, request_class=AuthorizationRequest)
        if isinstance(info, Response):
            satosa_logging(LOGGER, logging.ERROR, "Error in authn req: {}".format(info.message),
                           context.state)
            return info

        client_id = info["areq"]["client_id"]

        context.state.add(self.state_id, {"oidc_request": request})
        hash_type = oidc_subject_type_to_hash_type(
                self.provider.cdb[client_id].get("subject_type", self.subject_type_default))
        internal_req = InternalRequest(hash_type, client_id,
                                       self.provider.cdb[client_id].get("client_name"))

        return self.auth_req_callback_func(context, internal_req)

    def _jwks(self, context):
        """
        Construct the JWKS document (served at /jwks).
        :type context: satosa.context.Context
        :rtype: oic.utils.http_util.Response

        :param context: the current context
        :return: HTTP response to the client
        """
        return Response(json.dumps(self.provider.keyjar.export_jwks()), content="application/json")
