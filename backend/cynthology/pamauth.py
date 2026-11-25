"""Minimal PAM password check via ctypes (no third-party module needed)."""
from ctypes import (CDLL, CFUNCTYPE, POINTER, Structure, byref, c_char_p, c_int, c_void_p,
                    cast, sizeof, c_size_t)
from ctypes.util import find_library

_pam = CDLL(find_library("pam") or "libpam.so.0")
_libc = CDLL(find_library("c"))
_libc.calloc.restype = c_void_p
_libc.calloc.argtypes = [c_size_t, c_size_t]
_libc.strdup.restype = c_void_p
_libc.strdup.argtypes = [c_char_p]

PAM_PROMPT_ECHO_OFF, PAM_PROMPT_ECHO_ON = 1, 2


class PamMessage(Structure):
    _fields_ = [("msg_style", c_int), ("msg", c_char_p)]


class PamResponse(Structure):
    _fields_ = [("resp", c_void_p), ("resp_retcode", c_int)]


CONV_FUNC = CFUNCTYPE(c_int, c_int, POINTER(POINTER(PamMessage)), POINTER(POINTER(PamResponse)), c_void_p)


class PamConv(Structure):
    _fields_ = [("conv", CONV_FUNC), ("appdata_ptr", c_void_p)]


_pam.pam_start.argtypes = [c_char_p, c_char_p, POINTER(PamConv), POINTER(c_void_p)]
_pam.pam_authenticate.argtypes = [c_void_p, c_int]
_pam.pam_acct_mgmt.argtypes = [c_void_p, c_int]
_pam.pam_end.argtypes = [c_void_p, c_int]


def authenticate(username: str, password: str, service: str = "login") -> bool:
    pw = password.encode()

    @CONV_FUNC
    def conv(n, msgs, resp, _data):
        addr = _libc.calloc(n, sizeof(PamResponse))
        resp[0] = cast(addr, POINTER(PamResponse))
        for i in range(n):
            if msgs[i].contents.msg_style in (PAM_PROMPT_ECHO_OFF, PAM_PROMPT_ECHO_ON):
                resp[0][i].resp = _libc.strdup(pw)
                resp[0][i].resp_retcode = 0
        return 0

    handle = c_void_p()
    if _pam.pam_start(service.encode(), username.encode(), byref(PamConv(conv, None)), byref(handle)) != 0:
        return False
    try:
        return _pam.pam_authenticate(handle, 0) == 0 and _pam.pam_acct_mgmt(handle, 0) == 0
    finally:
        _pam.pam_end(handle, 0)
