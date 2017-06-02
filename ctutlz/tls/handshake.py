# from ctutlz.tls.handshake import cert_of_domain, scts_from_cert

import binascii
import socket
import struct
from functools import reduce

import certifi
import OpenSSL
import pyasn1_modules.rfc5280
from pyasn1.codec.der.decoder import decode as der_decoder
from pyasn1.type.univ import OctetString
from utlz import flo

from ctutlz.sct.sct import Sct
from ctutlz.tls.sctlist import SignedCertificateTimestampList


def create_context(scts_tls, scts_ocsp):
    '''
    Args:
        scts_tls: If True, register callback for TSL extension 18 (for SCTs)
        scts_ocsp: If True, register callback for OCSP-response (for SCTs)
    '''

    def verify_callback(conn, cert, errnum, depth, ok):
        '''Dummy callback.'''
        return 1  # True

    ctx = OpenSSL.SSL.Context(OpenSSL.SSL.SSLv23_METHOD)
    ctx.set_options(OpenSSL.SSL.OP_NO_SSLv2)
    ctx.set_options(OpenSSL.SSL.OP_NO_SSLv3)

    ctx.set_verify(OpenSSL.SSL.VERIFY_PEER, verify_callback)
    ca_filename = certifi.where()
    ctx.load_verify_locations(ca_filename)

    ctx.tls_extension_18_tdf = None
    if scts_tls:
        from ctutlz.tls.handshake_openssl import ffi, lib

        # this annotation makes the callback function available at
        # lib.serverinfo_cli_parse_cb() of type cdef so it can be used as
        # argument for the call of lib.SSL_CTX_add_client_custom_ext()
        @ffi.def_extern()
        def serverinfo_cli_parse_cb(ssl, ext_type, _in, inlen, al, arg):
            if ext_type == 18:

                def reduce_func(accum_value, current):
                    fmt = accum_value[0] + current[0]
                    values = accum_value[1] + (current[1], )
                    return fmt, values

                initializer = ('!', ())
                fmt, values = reduce(reduce_func, [
                    ('H', ext_type),
                    ('H', inlen),
                    (flo('{inlen}s'), bytes(ffi.buffer(_in, inlen))),
                ], initializer)
                ctx.tls_extension_18_tdf = struct.pack(fmt, *values)
            return 1  # True

        # register callback for TLS extension result into the SSL context
        # created with PyOpenSSL, using OpenSSL "directly"
        if not lib.SSL_CTX_add_client_custom_ext(ffi.cast('struct ssl_ctx_st *',
                                                          ctx._context),
                                                 18,
                                                 ffi.NULL, ffi.NULL, ffi.NULL,
                                                 lib.serverinfo_cli_parse_cb,
                                                 ffi.NULL):
            import sys
            sys.stderr.write('Unable to add custom extension 18\n')
            lib.ERR_print_errors_fp(sys.stderr)
            sys.exit(1)

    ctx.ocsp_resps = []
    if scts_ocsp:

        def ocsp_client_callback(connection, ocsp_data, data):
            ctx.ocsp_resps.append(ocsp_data)
            return True

        ctx.set_ocsp_client_callback(ocsp_client_callback, data=None)

    return ctx


def create_socket(scts_tls, scts_ocsp):
    '''
    Args:
        scts_tls: If True, register callback for TSL extension 18 (for SCTs)
        scts_ocsp: If True, register callback for OCSP-response (for SCTs)
    '''
    ctx = create_context(scts_tls, scts_ocsp)
    raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    return OpenSSL.SSL.Connection(ctx, raw_sock)


# formerly cert_of_domain
def do_handshake(domain, scts_tls, scts_ocsp):
    '''
    Args:
        domain: string with domain name,
                for example: 'ritter.vg', or 'www.ritter.vg'
        scts_tls: If True, register callback for TSL extension 18 (for SCTs)
        scts_ocsp: If True, register callback for OCSP-response (for SCTs)
    '''
    sock = create_socket(scts_tls, scts_ocsp)

    sock.request_ocsp()

    ocsp_response = None
    tls_extension_18_tdf = None

    try:
        sock.connect((domain, 443))
        sock.do_handshake()
        chain = sock.get_peer_cert_chain()
        certificate = sock.get_peer_certificate()

        ctx = sock.get_context()
        if scts_tls:
            if ctx.tls_extension_18_tdf:
                tls_extension_18_tdf = ctx.tls_extension_18_tdf
        if scts_ocsp:
            if ctx.ocsp_resps:
                ocsp_response = ctx.ocsp_resps[0]

    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        exc_type = type(exc)
        print(flo('### {exc}\n\n{tb}\n\nexc-type: {exc_type}'))
        raise exc
    finally:
        sock.close()  # sock.close() possible?

    return certificate, chain, ocsp_response, tls_extension_18_tdf


def cert_of_domain(domain, scts_tls=True, scts_ocsp=True):
    '''
    Args:
        domain: string with domain name,
                for example: 'ritter.vg', or 'www.ritter.vg'
        scts_tls: If True, register callback for TSL extension 18 (for SCTs)
        scts_ocsp: If True, register callback for OCSP-response (for SCTs)
    '''
    cert_x509, chain_x509s, ocsp_resp_der, tls_ext_18_tdf = do_handshake(
        domain, scts_tls, scts_ocsp)

    cert_der = OpenSSL.crypto.dump_certificate(
        type=OpenSSL.crypto.FILETYPE_ASN1, cert=cert_x509)
    # https://tools.ietf.org/html/rfc5246#section-7.4.2
    issuer_cert_x509 = chain_x509s[1]
    issuer_cert_der = OpenSSL.crypto.dump_certificate(
        type=OpenSSL.crypto.FILETYPE_ASN1, cert=issuer_cert_x509)
    return cert_der, issuer_cert_der, ocsp_resp_der, tls_ext_18_tdf


def scts_from_cert(cert_der):
    cert, _ = der_decoder(cert_der,
                          asn1Spec=pyasn1_modules.rfc5280.Certificate())
    scts = []

    exts = [extension
            for extension
            in cert['tbsCertificate']['extensions']
            if str(extension['extnID']) == '1.3.6.1.4.1.11129.2.4.2']

    if len(exts) != 0:
        extension_sctlist = exts[0]

        os_inner_der = extension_sctlist['extnValue']  # type: OctetString()
        os_inner, _ = der_decoder(os_inner_der, OctetString())

        sctlist_hex = os_inner.prettyPrint().split('0x')[-1]
        sctlist_der = binascii.unhexlify(sctlist_hex)
        sctlist = SignedCertificateTimestampList(sctlist_der)
        scts = [Sct(entry.sct_der) for entry in sctlist.sct_list]

    return scts
