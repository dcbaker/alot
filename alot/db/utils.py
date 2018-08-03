# encoding=utf-8
# Copyright (C) 2011-2012  Patrick Totzke <patricktotzke@gmail.com>
# Copyright © 2017 Dylan Baker <dylan@pnwbakers.com>
# This file is released under the GNU GPL, version 3 or a later revision.
# For further details see the COPYING file
import os
import email
import email.charset as charset
from email.header import Header
from email.iterators import typed_subpart_iterator
import email.policy
import email.utils
import tempfile
import re
import logging
import mailcap
import io
import base64
import quopri

from .. import crypto
from .. import helper
from ..errors import GPGProblem
from ..settings.const import settings
from ..helper import string_sanitize
from ..helper import string_decode
from ..helper import parse_mailcap_nametemplate
from ..helper import split_commandstring

charset.add_charset('utf-8', charset.QP, charset.QP, 'utf-8')

X_SIGNATURE_VALID_HEADER = 'X-Alot-OpenPGP-Signature-Valid'
X_SIGNATURE_MESSAGE_HEADER = 'X-Alot-OpenPGP-Signature-Message'

_APP_PGP_SIG = 'application/pgp-signature'
_APP_PGP_ENC = 'application/pgp-encrypted'


def add_signature_headers(mail, sigs, error_msg):
    '''Add pseudo headers to the mail indicating whether the signature
    verification was successful.

    :param mail: :class:`email.message.Message` the message to entitle
    :param sigs: list of :class:`gpg.results.Signature`
    :param error_msg: An error message if there is one, or None
    :type error_msg: :class:`str` or `None`
    '''
    sig_from = ''
    sig_known = True
    uid_trusted = False

    assert error_msg is None or isinstance(error_msg, str)

    if not sigs:
        error_msg = error_msg or u'no signature found'
    elif not error_msg:
        try:
            key = crypto.get_key(sigs[0].fpr)
            for uid in key.uids:
                if crypto.check_uid_validity(key, uid.email):
                    sig_from = uid.uid
                    uid_trusted = True
                    break
            else:
                # No trusted uid found, since we did not break from the loop.
                sig_from = key.uids[0].uid
        except GPGProblem:
            sig_from = sigs[0].fpr
            sig_known = False

    if error_msg:
        msg = 'Invalid: {}'.format(error_msg)
    elif uid_trusted:
        msg = 'Valid: {}'.format(sig_from)
    else:
        msg = 'Untrusted: {}'.format(sig_from)

    mail.add_header(X_SIGNATURE_VALID_HEADER,
                    'False' if (error_msg or not sig_known) else 'True')
    mail.add_header(X_SIGNATURE_MESSAGE_HEADER, msg)


def get_params(mail, failobj=None, header='content-type', unquote=True):
    '''Get Content-Type parameters as dict.

    RFC 2045 specifies that parameter names are case-insensitive, so
    we normalize them here.

    :param mail: :class:`email.message.Message`
    :param failobj: object to return if no such header is found
    :param header: the header to search for parameters, default
    :param unquote: unquote the values
    :returns: a `dict` containing the parameters
    '''
    failobj = failobj or []
    return {k.lower(): v for k, v in mail.get_params(failobj, header, unquote)}


def _handle_signatures(original, message, params):
    """Shared code for handling message signatures.

    RFC 3156 is quite strict:
    * exactly two messages
    * the second is of type 'application/pgp-signature'
    * the second contains the detached signature

    :param original: The original top-level mail. This is required to attache
        special headers to
    :type original: :class:`email.message.Message`
    :param message: The multipart/signed payload to verify
    :type message: :class:`email.message.Message`
    :param params: the message parameters as returned by :func:`get_params`
    :type params: dict[str, str]
    """
    malformed = None
    if len(message.get_payload()) != 2:
        malformed = u'expected exactly two messages, got {0}'.format(
            len(message.get_payload()))
    else:
        ct = message.get_payload(1).get_content_type()
        if ct != _APP_PGP_SIG:
            malformed = u'expected Content-Type: {0}, got: {1}'.format(
                _APP_PGP_SIG, ct)

    # TODO: RFC 3156 says the alg has to be lower case, but I've seen a message
    # with 'PGP-'. maybe we should be more permissive here, or maybe not, this
    # is crypto stuff...
    if not params.get('micalg', 'nothing').startswith('pgp-'):
        malformed = u'expected micalg=pgp-..., got: {0}'.format(
            params.get('micalg', 'nothing'))

    sigs = []
    if not malformed:
        try:
            sigs = crypto.verify_detached(
                message.get_payload(0).as_bytes(policy=email.policy.SMTP),
                message.get_payload(1).get_payload(decode=True))
        except GPGProblem as e:
            malformed = str(e)

    add_signature_headers(original, sigs, malformed)


def _handle_encrypted(original, message):
    """Handle encrypted messages helper.

    RFC 3156 is quite strict:
    * exactly two messages
    * the first is of type 'application/pgp-encrypted'
    * the first contains 'Version: 1'
    * the second is of type 'application/octet-stream'
    * the second contains the encrypted and possibly signed data

    :param original: The original top-level mail. This is required to attache
        special headers to
    :type original: :class:`email.message.Message`
    :param message: The multipart/signed payload to verify
    :type message: :class:`email.message.Message`
    """
    malformed = False

    ct = message.get_payload(0).get_content_type()
    if ct != _APP_PGP_ENC:
        malformed = u'expected Content-Type: {0}, got: {1}'.format(
            _APP_PGP_ENC, ct)

    want = 'application/octet-stream'
    ct = message.get_payload(1).get_content_type()
    if ct != want:
        malformed = u'expected Content-Type: {0}, got: {1}'.format(want, ct)

    if not malformed:
        # This should be safe because PGP uses US-ASCII characters only
        payload = message.get_payload(1).get_payload().encode('ascii')
        try:
            sigs, d = crypto.decrypt_verify(payload)
        except GPGProblem as e:
            # signature verification failures end up here too if the combined
            # method is used, currently this prevents the interpretation of the
            # recovered plain text mail. maybe that's a feature.
            malformed = str(e)
        else:
            n = decrypted_message_from_bytes(d)

            # add the decrypted message to message. note that n contains all
            # the attachments, no need to walk over n here.
            original.attach(n)

            original.defects.extend(n.defects)

            # there are two methods for both signed and encrypted data, one is
            # called 'RFC 1847 Encapsulation' by RFC 3156, and one is the
            # 'Combined method'.
            if not sigs:
                # 'RFC 1847 Encapsulation', the signature is a detached
                # signature found in the recovered mime message of type
                # multipart/signed.
                if X_SIGNATURE_VALID_HEADER in n:
                    for k in (X_SIGNATURE_VALID_HEADER,
                              X_SIGNATURE_MESSAGE_HEADER):
                        original[k] = n[k]
            else:
                # 'Combined method', the signatures are returned by the
                # decrypt_verify function.

                # note that if we reached this point, we know the signatures
                # are valid. if they were not valid, the else block of the
                # current try would not have been executed
                add_signature_headers(original, sigs, '')

    if malformed:
        msg = u'Malformed OpenPGP message: {0}'.format(malformed)
        content = email.message_from_string(msg)
        content.set_charset('utf-8')
        original.attach(content)


def decrypted_message_from_file(handle):
    '''Reads a mail from the given file-like object and returns an email
    object, very much like email.message_from_file. In addition to
    that OpenPGP encrypted data is detected and decrypted. If this
    succeeds, any mime messages found in the recovered plaintext
    message are added to the returned message object.

    :param handle: a file-like object
    :returns: :class:`email.message.Message` possibly augmented with
              decrypted data
    '''
    return decrypted_message_from_message(email.message_from_file(handle))


def decrypted_message_from_message(m):
    '''Detect and decrypt OpenPGP encrypted data in an email object. If this
    succeeds, any mime messages found in the recovered plaintext
    message are added to the returned message object.

    :param m: an email object
    :returns: :class:`email.message.Message` possibly augmented with
              decrypted data
    '''
    # make sure no one smuggles a token in (data from m is untrusted)
    del m[X_SIGNATURE_VALID_HEADER]
    del m[X_SIGNATURE_MESSAGE_HEADER]

    if m.is_multipart():
        p = get_params(m)

        # handle OpenPGP signed data
        if (m.get_content_subtype() == 'signed' and
                p.get('protocol') == _APP_PGP_SIG):
            _handle_signatures(m, m, p)

        # handle OpenPGP encrypted data
        elif (m.get_content_subtype() == 'encrypted' and
              p.get('protocol') == _APP_PGP_ENC and
              'Version: 1' in m.get_payload(0).get_payload()):
            _handle_encrypted(m, m)

        # It is also possible to put either of the abov into a multipart/mixed
        # segment
        elif m.get_content_subtype() == 'mixed':
            sub = m.get_payload(0)

            if sub.is_multipart():
                p = get_params(sub)

                if (sub.get_content_subtype() == 'signed' and
                        p.get('protocol') == _APP_PGP_SIG):
                    _handle_signatures(m, sub, p)
                elif (sub.get_content_subtype() == 'encrypted' and
                      p.get('protocol') == _APP_PGP_ENC):
                    _handle_encrypted(m, sub)

    return m


def decrypted_message_from_string(s):
    '''Reads a mail from the given string. This is the equivalent of
    :func:`email.message_from_string` which does nothing but to wrap
    the given string in a StringIO object and to call
    :func:`email.message_from_file`.

    Please refer to the documentation of :func:`message_from_file` for
    details.

    '''
    return decrypted_message_from_file(io.StringIO(s))


def decrypted_message_from_bytes(bytestring):
    """Create a Message from bytes.

    :param bytes bytestring: an email message as raw bytes
    """
    return decrypted_message_from_message(email.message_from_bytes(bytestring))


def extract_headers(mail, headers=None):
    """
    returns subset of this messages headers as human-readable format:
    all header values are decoded, the resulting string has
    one line "KEY: VALUE" for each requested header present in the mail.

    :param mail: the mail to use
    :type mail: :class:`email.Message`
    :param headers: headers to extract
    :type headers: list of str
    """
    headertext = u''
    if headers is None:
        headers = mail.keys()
    for key in headers:
        value = u''
        if key in mail:
            value = decode_header(mail.get(key, ''))
        headertext += '%s: %s\n' % (key, value)
    return headertext


def render_part(part, field_key='copiousoutput'):
    """
    renders a non-multipart email part into displayable plaintext by piping its
    payload through an external script. The handler itself is determined by
    the mailcap entry for this part's ctype.
    """
    ctype = part.get_content_type()
    raw_payload = remove_cte(part)
    rendered_payload = None
    # get mime handler
    _, entry = settings.mailcap_find_match(ctype, key=field_key)
    if entry is not None:
        tempfile_name = None
        stdin = None
        handler_raw_commandstring = entry['view']
        # in case the mailcap defined command contains no '%s',
        # we pipe the files content to the handling command via stdin
        if '%s' in handler_raw_commandstring:
            # open tempfile, respect mailcaps nametemplate
            nametemplate = entry.get('nametemplate', '%s')
            prefix, suffix = parse_mailcap_nametemplate(nametemplate)
            with tempfile.NamedTemporaryFile(
                    delete=False, prefix=prefix, suffix=suffix) \
                    as tmpfile:
                tmpfile.write(raw_payload)
                tempfile_name = tmpfile.name
        else:
            stdin = raw_payload

        # read parameter, create handler command
        parms = tuple('='.join(p) for p in part.get_params())

        # create and call external command
        cmd = mailcap.subst(entry['view'], ctype,
                            filename=tempfile_name, plist=parms)
        logging.debug('command: %s', cmd)
        logging.debug('parms: %s', str(parms))
        cmdlist = split_commandstring(cmd)
        # call handler
        stdout, _, _ = helper.call_cmd(cmdlist, stdin=stdin)
        if stdout:
            rendered_payload = stdout

        # remove tempfile
        if tempfile_name:
            os.unlink(tempfile_name)

    return rendered_payload


def remove_cte(part, as_string=False):
    """Decodes any Content-Transfer-Encodings.

    Can return a string for display, or bytes to be passed to an external
    program.

    :param email.Message part: The part to decode
    :param bool as_string: If true return a str, otherwise return bytes
    :returns: The mail with any Content-Transfer-Encoding removed
    :rtype: Union[str, bytes]
    """
    enc = part.get_content_charset() or 'ascii'
    cte = str(part.get('content-transfer-encoding', '7bit')).lower()
    payload = part.get_payload()
    if cte == '8bit':
        # Python's mail library may decode 8bit as raw-unicode-escape, so
        # we need to encode that back to bytes so we can decode it using
        # the correct encoding, or it might not, in which case assume that
        # the str representation we got is correct.
        raw_payload = payload.encode('raw-unicode-escape')
        if not as_string:
            return raw_payload
        try:
            return raw_payload.decode(enc)
        except LookupError:
            # In this case the email has an unknown encoding, fall back to
            # guessing
            return helper.try_decode(raw_payload)
        except UnicodeDecodeError:
            if not as_string:
                return raw_payload
            return helper.try_decode(raw_payload)
    elif cte in ['7bit', 'binary']:
        if as_string:
            return payload
        return payload.encode('utf-8')
    else:
        if cte == 'quoted-printable':
            raw_payload = quopri.decodestring(payload.encode('ascii'))
        elif cte == 'base64':
            raw_payload = base64.b64decode(payload)
        else:
            raise Exception(
                'Unknown Content-Transfer-Encoding: "{}"'.format(cte))
        # message.get_payload(decode=True) also handles a number of unicode
        # encodindigs. maybe those are useful?
        if not as_string:
            return raw_payload
        return raw_payload.decode(enc)
    raise Exception('Unreachable')


def extract_body(mail, types=None, field_key='copiousoutput'):
    """Returns a string view of a Message.

    If the `types` argument is set then any encoding types there will be used
    as the prefered encoding to extract. If `types` is None then
    :ref:`prefer_plaintext <prefer-plaintext>` will be consulted; if it is True
    then text/plain parts will be returned, if it is false then text/html will
    be returned if present or text/plain if there are no text/html parts.

    :param mail: the mail to use
    :type mail: :class:`email.Message`
    :param types: mime content types to use for body string
    :type types: list[str]
    :returns: The combined text of any parts to be used
    :rtype: str
    """

    preferred = 'text/plain' if settings.get(
        'prefer_plaintext') else 'text/html'
    has_preferred = False

    # see if the mail has our preferred type
    if types is None:
        has_preferred = list(typed_subpart_iterator(
            mail, *preferred.split('/')))

    body_parts = []
    for part in mail.walk():
        # skip non-leaf nodes in the mail tree
        if part.is_multipart():
            continue

        ctype = part.get_content_type()

        if types is not None:
            if ctype not in types:
                continue
        cd = part.get('Content-Disposition', '')
        if cd.startswith('attachment'):
            continue
        # if the mail has our preferred type, we only keep this type
        # note that if types != None, has_preferred always stays False
        if has_preferred and ctype != preferred:
            continue

        if ctype == 'text/plain':
            body_parts.append(string_sanitize(remove_cte(part, as_string=True)))
        else:
            rendered_payload = render_part(part)
            if rendered_payload:  # handler had output
                body_parts.append(string_sanitize(rendered_payload))
            else:  # mark as attachment
                part.add_header('Content-Disposition', 'attachment; ' + cd)
    return u'\n\n'.join(body_parts)


def decode_header(header, normalize=False):
    """
    decode a header value to a unicode string

    values are usually a mixture of different substrings
    encoded in quoted printable using different encodings.
    This turns it into a single unicode string

    :param header: the header value
    :type header: str
    :param normalize: replace trailing spaces after newlines
    :type normalize: bool
    :rtype: str
    """
    # some mailers send out incorrectly escaped headers
    # and double quote the escaped realname part again. remove those
    # RFC: 2047
    regex = r'"(=\?.+?\?.+?\?[^ ?]+\?=)"'
    value = re.sub(regex, r'\1', header)
    logging.debug("unquoted header: |%s|", value)

    # otherwise we interpret RFC2822 encoding escape sequences
    valuelist = email.header.decode_header(value)
    decoded_list = []
    for v, enc in valuelist:
        v = string_decode(v, enc)
        decoded_list.append(string_sanitize(v))
    value = ''.join(decoded_list)
    if normalize:
        value = re.sub(r'\n\s+', r' ', value)
    return value


def encode_header(key, value):
    """
    encodes a unicode string as a valid header value

    :param key: the header field this value will be stored in
    :type key: str
    :param value: the value to be encoded
    :type value: unicode
    """
    # handle list of "realname <email>" entries separately
    if key.lower() in ['from', 'to', 'cc', 'bcc']:
        rawentries = email.utils.getaddresses([value])
        encodedentries = []
        for name, address in rawentries:
            # try to encode as ascii, if that fails, revert to utf-8
            # name must be a unicode string here
            namepart = Header(name)
            # append address part encoded as ascii
            entry = email.utils.formataddr((namepart.encode(), address))
            encodedentries.append(entry)
        value = Header(', '.join(encodedentries))
    else:
        value = Header(value)
    return value.encode()


def is_subdir_of(subpath, superpath):
    # make both absolute
    superpath = os.path.realpath(superpath)
    subpath = os.path.realpath(subpath)

    # return true, if the common prefix of both is equal to directory
    # e.g. /a/b/c/d.rst and directory is /a/b, the common prefix is /a/b
    return os.path.commonprefix([subpath, superpath]) == superpath
