###############################################################################
#
# minipb.py
#
# SPDX-License-Identifier: BSD-3-Clause
#

"""
Mini Protobuf library

minipb uses simple schema representation to serialize and deserialize data
between Python data types and Protobuf binary wire messages.
Compare to original Protobuf, it is more light-weight, simple and therefore
can be used in resource limited systems, quick protocol prototyping and
reverse-engineering of unknown Protobuf messages.
"""

import re
import struct
import io

__all__ = [
    'BadFormatString', 'CodecError', 'EndOfMessage',
    'Wire', 'RawWire',
    'encode', 'decode', 'encode_raw', 'decode_raw',
]

_IS_MPY = __import__('sys').implementation.name == 'micropython'

class BadFormatString(ValueError):
    """
    Malformed format string
    """
    pass


class CodecError(Exception):
    """
    Error during serializing or deserializing
    """
    pass


class EndOfMessage(EOFError):
    """
    Reached end of Protobuf message while deserializing fields.
    """
    @property
    def partial(self):
        """
        True if the data was partially read.
        """
        if len(self.args) > 0:
            return self.args[0]
        else:
            return False


if _IS_MPY:
    # MicroPython re hack
    def _get_length_of_match(m):
        return len(m.group(0))
else:
    def _get_length_of_match(m):
        return m.end()

class BytesView:
    def __init__(self, buf, length):
        self.buf = buf
        self.length = length

    def tell(self):
        return self.buf.tell()

    def readinto(self, buf):
        # we don't do partial reads
        # but it's only used with a length of 1
        # so it's fine
        remaining = self.length
        if remaining >= len(buf):
            read = self.buf.readinto(buf)
            self.length -= read
            return read
        else:
            return 0

    def read(self, length=None):
        if not length:
            length = self.length
        readlen = min(length, self.length)
        if readlen > 0:
            res = self.buf.read(readlen)
            self.length -= len(res)
            return res
        else:
            return b""


class Wire(object):
    # Field types
    FIELD_WIRE_TYPE = {
        'x': None,
        'i': 5, 'I': 5, 'q': 1, 'Q': 1, 'f': 5, 'd': 1,
        'a': 2, 'b': 0, 'z': 0, 't': 0, 'T': 0, 'U': 2,
    }
    # Field aliases
    FIELD_ALIAS = {
        'v': 'z', 'V': 'T',
        'l': 'i', 'L': 'I'
    }

    # wire type & # of repeat
    _T_FMT = re.compile(
        r"^(?:({0})|({1}))(\d*)".format(
            '|'.join(FIELD_WIRE_TYPE.keys()),
            '|'.join(FIELD_ALIAS.keys())
        )
    )

    # Group 1: required/repeated/packed repeated, 2: nested struct begin
    _T_PREFIX = re.compile(r'^([\*\+#]?)(\[?)')

    # The default maximum length of a negative vint encoded in 2's complement (in bits)
    VINT_MAX_BITS = 64

    def __init__(self, fmt):
        self._vint_2sc_max_bits = 0
        self._vint_2sc_mask = 0
        self.vint_2sc_max_bits = self.__class__.VINT_MAX_BITS

        if isinstance(fmt, str):
            self._fmt = self._parse(fmt)
            self._kv_fmt = False
        else:
            self._fmt = self._parse_kvfmt(fmt)
            self._kv_fmt = True

    @property
    def vint_2sc_max_bits(self):
        """
        The maximum number of bits a signed 2's complement vint can contain.
        """
        return self._vint_2sc_max_bits

    @vint_2sc_max_bits.setter
    def vint_2sc_max_bits(self, bits):
        self._vint_2sc_max_bits = bits
        self._vint_2sc_mask = (1 << bits) - 1

    @property
    def kvfmt(self):
        """
        True if the object works in key-value format list (kvfmt) mode.
        """
        return self._kv_fmt

    def _parse_kvfmt(self, fmtlist):
        """
        Similar to _parse() but for key-value format lists.
        """
        t_fmt = self.__class__._T_FMT
        t_prefix = self.__class__._T_PREFIX
        parsed_list = []
        field_id = 1

        for entry in fmtlist:
            name = entry[0]
            fmt = entry[1]
            parsed_field = {}
            parsed_field['name'] = name
            if isinstance(fmt, str):
                ptr = 0
                m_prefix = t_prefix.match(fmt)
                if m_prefix:
                    ptr += _get_length_of_match(m_prefix)
                    parsed_field['prefix'] = m_prefix.group(1)
                    # check for optional nested structure start (required if the field is also repeated)
                    if m_prefix.group(2) and len(entry) > 2:
                        parsed_field['field_id'] = field_id
                        parsed_field['field_type'] = 'a'
                        parsed_field['subcontent'] = self._parse_kvfmt(entry[2])
                        field_id += 1
                        parsed_list.append(parsed_field)
                        continue
                    elif m_prefix.group(2):
                        raise BadFormatString('Nested field type used without specifying field format.')
                m_fmt = t_fmt.match(fmt[ptr:])
                if m_fmt:
                    ptr += _get_length_of_match(m_fmt)
                    resolved_fmt_char = None
                    # fmt is an alias
                    if m_fmt.group(2):
                        resolved_fmt_char = m_fmt.group(2)
                        parsed_field['field_type'] = self.__class__\
                            .FIELD_ALIAS[m_fmt.group(2)]
                    # fmt is an actual field type
                    elif m_fmt.group(1):
                        resolved_fmt_char = m_fmt.group(1)
                        parsed_field['field_type'] = m_fmt.group(1)
                    parsed_field['field_id'] = field_id
                    # only skip type (`x') is allowed for copying in key-value mode
                    if m_fmt.group(3) and resolved_fmt_char == 'x':
                        repeats = int(m_fmt.group(3))
                        parsed_field['repeat'] = repeats
                        field_id += repeats
                    elif m_fmt.group(3):
                        raise BadFormatString('Field copying is not allowed in key-value format list.')
                    else:
                        field_id += 1
                else:
                    raise BadFormatString('Invalid type for field "{0}"'.format(name))
                if len(fmt) != ptr:
                    print('Extra content found after the type string of %s.', name)
            else:
                # Hard-code the empty prefix because we don't support copying
                parsed_field['prefix'] = ''
                parsed_field['field_id'] = field_id
                parsed_field['field_type'] = 'a'
                parsed_field['subcontent'] = self._parse_kvfmt(fmt)
                field_id += 1
            parsed_list.append(parsed_field)
        return parsed_list

    def _parse(self, fmtstr):
        """
        Parse format string to something more machine readable.
        Called internally inside the class.
        Format of parsed format list:
            - field_id: The id (index) of the field.
            - field_type: Type of the field. (see the doc, FIELD_WIRE_TYPE and FIELD_ALIAS)
            - prefix: Prefix of the field. (required, repeated, packed-repeated) (EXCLUDES nested structures)
                      Needs to be an empty string when there's none.
            - subcontent: Optional. Used for nested structures. (field_type must be `a' when this is defined)
            - repeat: Optional. Copy this field specified number of times to consecutive indices.
        """
        def _match_brace(string, start_pos, pair='[]'):
            """Pairing brackets (used internally in _parse method)"""
            depth = 1
            if string[start_pos] != pair[0]:
                return None
            for index, char in enumerate(string[start_pos + 1:]):
                if char == pair[0]:
                    depth += 1
                elif char == pair[1]:
                    depth -= 1
                if depth == 0:
                    return start_pos + index + 1
            return None

        #----------------------------------------------------------------------

        t_fmt = self.__class__._T_FMT
        t_prefix = self.__class__._T_PREFIX

        ptr = 0
        # it seems that field id 0 is invalid
        field_id = 1
        length = len(fmtstr)
        parsed_list = []

        while ptr < length:
            parsed = {}
            m_prefix = t_prefix.match(fmtstr[ptr:])
            if m_prefix:
                ptr += _get_length_of_match(m_prefix)
                parsed['prefix'] = m_prefix.group(1)

                # check if we have a nested structure
                if m_prefix.group(2):
                    brace_offset = _match_brace(fmtstr, ptr - 1)

                    # bracket not match
                    if not brace_offset:
                        raise BadFormatString(
                            'Unmatched brace on position {0}'.format(ptr)
                        )
                    parsed['field_id'] = field_id
                    parsed['field_type'] = 'a'
                    parsed['subcontent'] = self._parse(
                        fmtstr[ptr:brace_offset]
                    )
                    ptr = brace_offset + 1
                    field_id += 1

                    parsed_list.append(parsed)
                    continue
            m_fmt = t_fmt.match(fmtstr[ptr:])
            if m_fmt:
                ptr += _get_length_of_match(m_fmt)

                # fmt is an alias
                if m_fmt.group(2):
                    parsed['field_type'] = self.__class__\
                        .FIELD_ALIAS[m_fmt.group(2)]
                # fmt is an actual field type
                elif m_fmt.group(1):
                    parsed['field_type'] = m_fmt.group(1)

                # save field id
                parsed['field_id'] = field_id

                # check for type clones (e.g. `v3')
                if m_fmt.group(3):
                    parsed['repeat'] = int(m_fmt.group(3))
                    field_id += int(m_fmt.group(3))
                else:
                    parsed['repeat'] = 1
                    field_id += 1

                parsed_list.append(parsed)

            else:
                raise BadFormatString(
                    'Invalid token on position {0}'.format(ptr)
                )

        # all set
        return parsed_list

    def encode(self, *stuff):
        """
        Encode given objects to binary wire format.
        If the Wire object was created using the key-value format list,
        the method accepts one dict object that contains all the objects
        to be encoded.
        Otherwise, the method accepts multiple objects (like Struct.pack())
        and all objects will be encoded sequentially.
        """
        if self._kv_fmt:
            result = self._encode_wire(stuff[0])
        else:
            result = self._encode_wire(stuff)
        return result.getvalue()

    def _encode_wire(self, stuff, fmtable=None):
        """
        Encode a list to binary wire using fmtable
        Returns a BytesIO object (not a str)
        Used by the encode() method, may also be invoked by _encode_field()
        to encode nested structures
        """
        if fmtable == None:
            fmtable = self._fmt

        # Can be a index number or field name
        stuff_id = 0
        encoded = io.BytesIO()
        for fmt in fmtable:
            if self._kv_fmt:
                assert 'name' in fmt, 'Encoder is in key-value mode but name is undefined for this field'
                stuff_id = fmt['name']
            field_id_start = fmt['field_id']
            field_type = fmt['field_type']
            repeat = fmt.get('repeat', 1)
            for field_id in range(field_id_start, field_id_start + repeat):
                try:
                    field_data = stuff[stuff_id]
                except (IndexError, KeyError):
                    raise CodecError('Insufficient parameters '
                                     '(empty field {0} not padded with None)'.format(
                                         fmt['name'] if self._kv_fmt else field_id))
                prefix = fmt['prefix']
                subcontent = fmt.get('subcontent')
                wire_type = self.__class__.FIELD_WIRE_TYPE[fmt['field_type']]

                # Skip blank field (placeholder)
                if field_type == 'x':
                    continue

                # Packed repeating field always has a str-like header
                if prefix == '#':
                    encoded_header = self._encode_header(
                        self.__class__.FIELD_WIRE_TYPE['a'],
                        field_id
                    )
                else:
                    encoded_header = self._encode_header(wire_type, field_id)

                # Empty required field
                if prefix == '*' and field_data == None:
                    raise CodecError('Required field cannot be None.')

                # Empty optional field
                if field_data == None:
                    if not self._kv_fmt:
                        stuff_id += 1
                    continue

                # repeating field
                if prefix == '+':
                    for obj in field_data:
                        encoded.write(encoded_header)
                        encoded.write(
                            self._encode_field(field_type, obj, subcontent)
                        )

                # packed repeating field
                elif prefix == '#':
                    packed_body = io.BytesIO()
                    for obj in field_data:
                        packed_body.write(self._encode_field(
                            field_type, obj, subcontent
                        ))
                    encoded.write(encoded_header)
                    encoded.write(self._encode_str(packed_body.getvalue()))

                # normal field
                else:
                    encoded.write(encoded_header)
                    encoded.write(
                        self._encode_field(field_type, field_data, subcontent)
                    )
                if not self._kv_fmt:
                    stuff_id += 1

        encoded.seek(0)
        return encoded

    def _encode_field(self, field_type, field_data, subcontent=None):
        """
        Encode a single field to binary wire format
        Called internally in _encode_wire() function
        """
        field_encoded = None

        # nested
        if field_type == 'a' and subcontent:
            field_encoded = self._encode_str(
                self._encode_wire(field_data, subcontent).read()
            )
        # bytes
        elif field_type == 'a':
            field_encoded = self._encode_str(field_data)

        # strings
        elif field_type == 'U':
            field_encoded = self._encode_str(field_data.encode('utf-8'))

        # vint family (signed, unsigned and boolean)
        elif field_type in 'Ttzb':
            if field_type == 't':
                field_data = self._vint_signedto2sc(field_data)
            elif field_type == 'z':
                field_data = self._vint_zigzagify(field_data)
            elif field_type == 'b':
                field_data = int(field_data)
            field_encoded = self._encode_vint(field_data)

        # fixed numerical value
        elif field_type in 'iIqQfd':
            field_encoded = struct.pack(
                '<{0}'.format(field_type), field_data
            )

        return field_encoded

    def _encode_header(self, f_type, f_id):
        """
        Encode a header
        Called internally in _encode_wire() function
        """
        hdr = (f_id << 3) | f_type
        return self._encode_vint(hdr)

    @staticmethod
    def _vint_zigzagify(number):
        """
        Perform zigzag encoding
        Called internally in _encode_field() function
        """
        num = number << 1
        if number < 0:
            num = ~num
        return num

    def _vint_signedto2sc(self, number):
        """
        Perform Two's Complement encoding
        Called internally in _encode_field() function
        """
        return number & self._vint_2sc_mask

    @staticmethod
    def _encode_vint(number):
        """
        Encode a number to vint (Wire Type 0).
        Numbers can only be signed or unsigned. Any number less than 0 must
        be processed either using zigzag or 2's complement (2sc) before
        passing to this function.
        Called internally in _encode_field() function
        """

        assert number >= 0, 'number is less than 0'
        result = bytearray()
        while 1:
            tmp = number & 0x7f
            number >>= 7
            if number == 0:
                result.append(tmp)
                break
            result.append(0x80 | tmp)
        return bytes(result)

    def _encode_str(self, string):
        """
        Encode a string/binary stream into protobuf variable length by
        appending a special header containing the length of the string.
        Called internally in _encode_field() function
        """
        result = self._encode_vint(len(string))
        result += string
        return result

    def decode(self, data):
        """Decode given binary wire data to Python data types."""

        # Tested:
        #   types: z, T, a
        #   nested_structure
        #   repeated
        if not hasattr(data, 'read'):
            data = io.BytesIO(data)

        if self._kv_fmt:
            return dict(self._decode_wire(data))
        else:
            return tuple(self._decode_wire(data))

    def _decode_header(self, buf):
        """
        Decode field header.
        Raises EndOfMessage if there is no or only partial data available.
        Called internally in decode() method
        """
        ord_data = self._decode_vint(buf)
        f_type = ord_data & 7
        f_id = ord_data >> 3
        return f_type, f_id

    @staticmethod
    def _decode_vint(buf):
        """
        Decode vint encoded integer.
        Raises EndOfMessage if there is no or only partial data available.
        Called internally in decode() method.
        """
        ctr = 0
        result = 0
        tmp = bytearray(1)
        partial = False
        while 1:
            count = buf.readinto(tmp)
            if count == 0:
                raise EndOfMessage(partial)
            else:
                partial = True
            result |= (tmp[0] & 0x7f) << (7 * ctr)
            if not (tmp[0] >> 7): break
            ctr += 1
        return result

    @staticmethod
    def _vint_dezigzagify(number):
        """
        Convert zigzag encoded integer to its original form.
        Called internally in _decode_field() function
        """

        assert number >= 0, 'number is less than 0'
        is_neg = number & 1
        num = number >> 1
        if is_neg:
            num = ~num
        return num

    def _vint_2sctosigned(self, number):
        """
        Decode Two's Complement encoded integer (which were treated by the
        'shallow' decoder as unsigned vint earlier) to normal signed integer
        Called internally in _decode_field() function
        """
        assert number >= 0, 'number is less than 0'
        if (number >> (self._vint_2sc_max_bits - 1)) & 1:
            number = ~(~number & self._vint_2sc_mask)
        return number

    def _decode_str(self, buf):
        """
        Decode Protobuf variable length string to Python string.
        Raises EndOfMessage if there is no or only partial data available.
        Called internally in _decode_field() function.
        """
        length = self._decode_vint(buf)
        result = buf.read(length)
        if len(result) != length:
            raise EndOfMessage(True)
        return result

    def _decode_str_ref(self, buf):
        """
        Decode Protobuf variable length string to an offset and length.
        Called internally in _decode_field() function.
        """
        length = self._decode_vint(buf)
        result = BytesView(buf, length)
        return result

    @staticmethod
    def _read_fixed(buf, length):
        """
        Read out a fixed type and report if the result is incomplete.
        Called internally in _break_down().
        """
        result = buf.read(length)
        actual = len(result)
        if actual != length:
            raise EndOfMessage(False if actual == 0 else True)
        return result

    def _break_down(self, buf, type_override=None, id_override=None, read_str=True):
        """
        Helper method to 'break down' a wire string into a list for
        further processing.
        Pass type_override and id_override to decompose headerless wire
        strings. (Mainly used for unpacking packed repeated fields)
        Called internally in _decode_wire() function
        """
        assert (id_override is not None and type_override is not None) or\
               (id_override is None and type_override is None),\
            'Field ID and type must be both specified in headerless mode'

        while True:
            field = {}
            if type_override is not None:
                f_type = type_override
                f_id = id_override
            else:
                # if no more data, stop and return
                try:
                    f_type, f_id = self._decode_header(buf)
                except EOFError:
                    break

            try:
                if f_type == 0: # vint
                    field['data'] = self._decode_vint(buf)
                elif f_type == 1: # 64-bit
                    field['data'] = self._read_fixed(buf, 8)
                elif f_type == 2: # str
                    field['data'] = self._decode_str(buf) if read_str else self._decode_str_ref(buf)
                elif f_type == 5: # 32-bit
                    field['data'] = self._read_fixed(buf, 4)
                else:
                    print(
                        "_break_down():Ignore unknown type #%d", f_type
                    )
                    continue
            except EndOfMessage as e:
                if type_override is None or e.partial:
                    raise CodecError('Unexpected end of message while decoding field {0}'.format(f_id))
                else:
                    break
            field['id'] = f_id
            field['wire_type'] = f_type
            yield field

    def _decode_field(self, field_type, field_data, subcontent=None, path=()):
        """
        Decode a single field
        Called internally in _decode_wire() function
        """
        # check wire type
        wt_schema = self.__class__.FIELD_WIRE_TYPE[field_type]
        wt_data = field_data['wire_type']
        if wt_schema != wt_data:
            raise TypeError(
                'Wire type mismatch (expect {0} but got {1})'\
                    .format(wt_schema, wt_data)
            )

        field_decoded = None

        fd_data = field_data['data']

        # the actual decoding process
        # nested structure
        if field_type == 'a' and subcontent:
            if hasattr(fd_data, 'read'):
                field_decoded = self._decode_wire(
                    fd_data,
                    subcontent,
                    path
                )
            elif self._kv_fmt:
                field_decoded = dict(self._decode_wire(
                    io.BytesIO(fd_data),
                    subcontent
                ))
            else:
                field_decoded = tuple(self._decode_wire(
                    io.BytesIO(fd_data),
                    subcontent
                ))

        # string, unsigned vint (2sc)
        elif field_type in 'aT':
            if hasattr(fd_data, 'read'):
                fd_data = fd_data.read()
            field_decoded = fd_data

        # unicode
        elif field_type in 'U':
            if hasattr(fd_data, 'read'):
                fd_data = fd_data.read()
            field_decoded = fd_data.decode('utf-8')

        # vint (zigzag)
        elif field_type == 'z':
            field_decoded = self._vint_dezigzagify(fd_data)

        # signed 2sc
        elif field_type == 't':
            field_decoded = self._vint_2sctosigned(fd_data)

        # fixed, float, double
        elif field_type in 'iIfdqQ':
            field_decoded = struct.unpack(
                '<{0}'.format(field_type), fd_data
            )[0]

        # boolean
        elif field_type == 'b':
            if fd_data == 0:
                field_decoded = False
            else:
                field_decoded = True

        return field_decoded

    def _decode_wire(self, buf, subfmt=None):
        """
        Apply schema, decode nested structure and fixed length data.
        Used by the decode() method, may also be invoked by _decode_field()
        to decode nested structures
        """
        def _concat_fields(fields):
            """
            Concatenate 2 fields with the same wire type together
            """
            result_wire = io.BytesIO()
            result = {'id': fields[0]['id'], 'wire_type': fields[0]['wire_type']}
            for field in fields:
                assert field['id'] == result['id'] and \
                    field['wire_type'] == result['wire_type'], \
                    'field id or wire_type mismatch'
                result_wire.write(field['data'])
            result['data'] = result_wire.getvalue()
            return result

        decoded_raw = tuple(self._break_down(buf))
        if not subfmt:
            subfmt = self._fmt

        for fmt in subfmt:
            field_id_start = fmt['field_id']
            field_type = fmt['field_type']
            field_prefix = fmt.get('prefix')
            subcontent = fmt.get('subcontent')
            repeat = fmt.get('repeat', 1)

            # sanity check
            if self._kv_fmt:
                assert repeat == 1 or field_type == 'x', 'Refuse to do field copying on non-skip field in key-value mode.'

            for field_id in range(field_id_start, field_id_start + repeat):

                # skip blank field
                if field_type == 'x':
                    continue

                # get all the data attached on the given field
                fields = tuple(x for x in decoded_raw if x['id'] == field_id)

                # raise error if a required field is empty
                if field_prefix == '*' and len(fields) == 0:
                    raise CodecError(
                        'Field {0} is required but is empty'\
                            .format(field_id)
                    )

                # identify which kind of repeated field is present
                # normal repeated fields
                if field_prefix == '+':
                    field_decoded = tuple(
                        self._decode_field(field_type, f, subcontent)
                        for f in fields
                    )

                # packed repeated field
                elif field_prefix == '#':
                    if len(fields) > 1:
                        print(
                            'Multiple data found in a packed-repeated field.'
                        )
                        fields = (_concat_fields(fields), )
                    if fields[0]['wire_type'] != self.__class__.FIELD_WIRE_TYPE['a']:
                        raise CodecError('Packed repeated field {0} has wire type other than str'.format(
                            fmt['name'] if self._kv_fmt else field_id
                        ))
                    field = io.BytesIO(fields[0]['data'])
                    unpacked_field = self._break_down(
                        field,
                        type_override=self.__class__.FIELD_WIRE_TYPE[field_type],
                        id_override=field_id
                    )
                    field_decoded = tuple(
                        self._decode_field(field_type, f, subcontent)
                        for f in unpacked_field
                    )

                # not a repeated field but has multiple data in one field
                elif len(fields) > 1:
                    print(
                        'Multiple data found in a non-repeated field.'
                    )
                    # Check if we are expecting a nested message
                    if subcontent is None:
                        # Use the last found data
                        field_decoded = self._decode_field(
                            field_type, fields[-1], subcontent
                        )
                    else:
                        # Concat all pieces of the nested message together and decode
                        #
                        # https://developers.google.com/protocol-buffers/docs/encoding#optional
                        # For embedded message fields, the parser merges multiple instances of the same field,
                        # as if with the `Message::MergeFrom` method – that is, all singular scalar fields in
                        # the latter instance replace those in the former, singular embedded messages are merged,
                        # and repeated fields are concatenated.
                        field_decoded = self._decode_field(
                            field_type, _concat_fields(fields), subcontent
                        )

                # not a repeated field
                else:
                    if len(fields) != 0:
                        field_decoded = self._decode_field(
                            field_type, fields[0], subcontent
                        )
                    else:
                        field_decoded = None

                if self._kv_fmt:
                    yield fmt['name'], field_decoded
                else:
                    yield field_decoded


class RawWire(Wire):
    '''
    This class exposes the internal encoding/decoding routines of the Wire class
    to allow raw wire data generating/parsing without the need of a schema
    It is useful for analyzing Protobuf messages with an unknown schema
    '''
    def __init__(self):
        pass

    def decode(self, data):
        '''
        Decode wire data to a list of dicts that contain raw wire data and types
        The dictionary contains 3 keys:
            - id: The field number that the data belongs to
            - wire_type: Wire type of that field, see
              https://developers.google.com/protocol-buffers/docs/encoding
              for the list of wire types (currently type 3 and 4 are not
              supported)
            - data: The raw data of the field. Note that data with wire type 0
              (vints) are always decoded as unsigned Two's Complement format
              regardless of ZigZag encoding was being used (which also means
              they will always be positive) and wire type 1 and 5 (fixed-length)
              are decoded as bytes of fixed length (i.e. 8 bytes for type 1 and
              4 bytes for type 5)
        '''
        if not hasattr(data, 'read'):
            data = io.BytesIO(data)

        return tuple(self._break_down(data))

    def encode(self, stuff):
        '''
        Encode the output of decode() back to binary wire format
        '''
        def _check_bytes_length(data, length):
            if not hasattr(data, 'decode'):
                raise ValueError(
                    'Excepted a bytes object, not {}'.format(
                        type(data).__name__
                    )
                )
            elif len(data) != length:
                raise ValueError(
                    'Excepted a bytes object of length {}, got {}'.format(
                        length, len(data)
                    )
                )
            return data

        ENCODERS = {
            0: self._encode_vint,
            1: lambda n: _check_bytes_length(n, 8),
            2: self._encode_str,
            5: lambda n: _check_bytes_length(n, 4)
        }
        encoded = io.BytesIO()
        for s in stuff:
            encoded.write(self._encode_header(s['wire_type'], s['id']))
            if s['wire_type'] not in ENCODERS.keys():
                raise ValueError('Unknown type {}'.format(s['wire_type']))
            encoded.write(ENCODERS[s['wire_type']](s['data']))

        return encoded.getvalue()


def bisect_field_id(a, x):
    # without repeats this should work
    mid = min(x-1, len(a)-1)
    lo = 0
    hi = len(a)
    while lo < hi:
        res = a[mid]
        fid = res['field_id']
        if fid == x: return res
        if fid < x: lo = mid+1
        else: hi = mid
        mid = (lo+hi)//2
    res = a[lo]
    if res['field_id'] == x:
        return res
    else:
        return a[lo-1]

tk_start = "__start__"
tk_end = "__end__"

class IterWire(Wire):
    '''
    This class is like Wire, but decode returns an iterator of (path, value) pairs.
    This is useful for decoding larger than memory Protobuf values.
    '''

    def _decode_wire(self, buf, subfmt, path=()):
        for field in self._break_down(buf, read_str=False):
            fmt = bisect_field_id(subfmt, field['id'])
            key = fmt.get('name') or fmt['field_id']
            mypath = path + (key,)
            # packed repeated field
            if fmt.get('prefix') == '#':
                if field['wire_type'] != self.__class__.FIELD_WIRE_TYPE['a']:
                    raise CodecError('Packed repeated field {} has wire type other than str'.format(key))
                typ = self.__class__.FIELD_WIRE_TYPE[fmt['field_type']]
                unpacked_field = self._break_down(field['data'], type_override=typ, id_override=fmt['field_id'])
                for f in unpacked_field:
                    res = self._decode_field(fmt['field_type'], f, fmt.get('subcontent'), mypath)
                    if hasattr(res, 'send'): # generator
                        yield mypath, tk_start
                        yield from res
                        yield mypath, tk_end
                    else:
                        yield mypath, res
            else:
                res = self._decode_field(fmt['field_type'], field, fmt.get('subcontent'), mypath)
                if hasattr(res, 'send'): # generator
                    yield mypath, tk_start
                    yield from res
                    yield mypath, tk_end
                else:
                    yield mypath, res

    def decode(self, data):
        if not hasattr(data, 'read'):
            data = io.BytesIO(data)
        return self._decode_wire(data, self._fmt)



def encode(fmtstr, *stuff):
    """Encode given Python object(s) to binary wire using fmtstr"""
    return Wire(fmtstr).encode(*stuff)

def decode(fmtstr, data):
    """Decode given binary wire to Python object(s) using fmtstr"""
    return Wire(fmtstr).decode(data)

def encode_raw(objs):
    """
    Encode a list of raw data and types to binary wire format
    Useful for analyzing Protobuf messages with unknown schema
    """
    return RawWire().encode(objs)

def decode_raw(data):
    """
    Decode given binary wire to a list of raw data and types
    Useful for analyzing Protobuf messages with unknown schema
    """
    return RawWire().decode(data)

if __name__ == '__main__':
    import sys
    import json
    def usage():
        """Isn't that obvious?"""
        print('Usage: {prog} <-d|-e> <fmtstr>'.format(prog=sys.argv[0]))
        sys.exit(1)

    if len(sys.argv) < 3:
        usage()
    if sys.argv[1] == '-d':
        json.dump(decode(sys.argv[2], sys.stdin.buffer), sys.stdout)
        sys.stdout.write("\n")
    elif sys.argv[1] == '-e':
        sys.stdout.buffer.write(encode(sys.argv[2], *json.load(sys.stdin)))
    else:
        usage()
