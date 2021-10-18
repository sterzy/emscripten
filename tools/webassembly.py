# Copyright 2011 The Emscripten Authors.  All rights reserved.
# Emscripten is available under two separate licenses, the MIT license and the
# University of Illinois/NCSA Open Source License.  Both these licenses can be
# found in the LICENSE file.

"""Utilties for manipulating WebAssembly binaries from python.
"""

from collections import namedtuple
from enum import IntEnum
import logging
import os
import sys

from . import utils

sys.path.append(utils.path_from_root('third_party'))

import leb128

logger = logging.getLogger('webassembly')

WASM_PAGE_SIZE = 65536

MAGIC = b'\0asm'

VERSION = b'\x01\0\0\0'

HEADER_SIZE = 8

LIMITS_HAS_MAX = 0x1


def toLEB(num):
  return leb128.u.encode(num)


def readULEB(iobuf):
  return leb128.u.decode_reader(iobuf)[0]


def readSLEB(iobuf):
  return leb128.i.decode_reader(iobuf)[0]


class SecType(IntEnum):
  CUSTOM = 0
  TYPE = 1
  IMPORT = 2
  FUNCTION = 3
  TABLE = 4
  MEMORY = 5
  TAG = 13
  GLOBAL = 6
  EXPORT = 7
  START = 8
  ELEM = 9
  DATACOUNT = 12
  CODE = 10
  DATA = 11


class ExternType(IntEnum):
  FUNC = 0
  TABLE = 1
  MEMORY = 2
  GLOBAL = 3
  TAG = 4


class DylinkType(IntEnum):
  MEM_INFO = 1
  NEEDED = 2
  EXPORT_INFO = 3


Section = namedtuple('Section', ['type', 'size', 'offset'])
Limits = namedtuple('Limits', ['flags', 'initial', 'maximum'])
Import = namedtuple('Import', ['kind', 'module', 'field'])
Export = namedtuple('Export', ['name', 'kind', 'index'])
Dylink = namedtuple('Dylink', ['mem_size', 'mem_align', 'table_size', 'table_align', 'needed', 'export_info'])


class Module:
  """Extremely minimal wasm module reader.  Currently only used
  for parsing the dylink section."""
  def __init__(self, filename):
    self.size = os.path.getsize(filename)
    self.buf = open(filename, 'rb')
    magic = self.buf.read(4)
    version = self.buf.read(4)
    assert magic == MAGIC
    assert version == VERSION

  def __del__(self):
    self.buf.close()

  def readByte(self):
    return self.buf.read(1)[0]

  def readULEB(self):
    return readULEB(self.buf)

  def readSLEB(self):
    return readSLEB(self.buf)

  def readString(self):
    size = self.readULEB()
    return self.buf.read(size).decode('utf-8')

  def readLimits(self):
    flags = self.readByte()
    initial = self.readULEB()
    maximum = 0
    if flags & LIMITS_HAS_MAX:
      maximum = self.readULEB()
    return Limits(flags, initial, maximum)

  def seek(self, offset):
    return self.buf.seek(offset)

  def tell(self):
    return self.buf.tell()

  def skip(self, count):
    self.buf.seek(count, os.SEEK_CUR)

  def sections(self):
    """Generator that lazily returns sections from the wasm file."""
    offset = HEADER_SIZE
    while offset < self.size:
      self.seek(offset)
      section_type = SecType(self.readByte())
      section_size = self.readULEB()
      section_offset = self.buf.tell()
      yield Section(section_type, section_size, section_offset)
      offset = section_offset + section_size

  def parse_dylink_section(self):
    dylink_section = next(self.sections())
    assert dylink_section.type == SecType.CUSTOM
    self.seek(dylink_section.offset)
    # section name
    section_name = self.readString()
    needed = []
    export_info = {}

    if section_name == 'dylink':
      mem_size = self.readULEB()
      mem_align = self.readULEB()
      table_size = self.readULEB()
      table_align = self.readULEB()

      needed_count = self.readULEB()
      while needed_count:
        libname = self.readString()
        needed.append(libname)
        needed_count -= 1
    elif section_name == 'dylink.0':
      section_end = dylink_section.offset + dylink_section.size
      while self.tell() < section_end:
        subsection_type = self.readULEB()
        subsection_size = self.readULEB()
        end = self.tell() + subsection_size
        if subsection_type == DylinkType.MEM_INFO:
          mem_size = self.readULEB()
          mem_align = self.readULEB()
          table_size = self.readULEB()
          table_align = self.readULEB()
        elif subsection_type == DylinkType.NEEDED:
          needed_count = self.readULEB()
          while needed_count:
            libname = self.readString()
            needed.append(libname)
            needed_count -= 1
        elif subsection_type == DylinkType.EXPORT_INFO:
          count = self.readULEB()
          while count:
            sym = self.readString()
            flags = self.readULEB()
            export_info[sym] = flags
            count -= 1
        else:
          print(f'unknown subsection: {subsection_type}')
          # ignore unknown subsections
          self.skip(subsection_size)
        assert(self.tell() == end)
    else:
      utils.exit_with_error('error parsing shared library')

    return Dylink(mem_size, mem_align, table_size, table_align, needed, export_info)

  def get_exports(self):
    export_section = next((s for s in self.sections() if s.type == SecType.EXPORT), None)
    if not export_section:
      return []

    self.seek(export_section.offset)
    num_exports = self.readULEB()
    exports = []
    for i in range(num_exports):
      name = self.readString()
      kind = ExternType(self.readByte())
      index = self.readULEB()
      exports.append(Export(name, kind, index))

    return exports

  def get_imports(self):
    import_section = next((s for s in self.sections() if s.type == SecType.IMPORT), None)
    if not import_section:
      return []

    self.seek(import_section.offset)
    num_imports = self.readULEB()
    imports = []
    for i in range(num_imports):
      mod = self.readString()
      field = self.readString()
      kind = ExternType(self.readByte())
      imports.append(Import(kind, mod, field))
      if kind == ExternType.FUNC:
        self.readULEB()  # sig
      elif kind == ExternType.GLOBAL:
        self.readSLEB()  # global type
        self.readByte()  # mutable
      elif kind == ExternType.MEMORY:
        self.readLimits()  # limits
      elif kind == ExternType.TABLE:
        self.readSLEB()  # table type
        self.readLimits()  # limits
      elif kind == ExternType.TAG:
        self.readByte()  # attribute
        self.readULEB()  # sig
      else:
        assert False

    return imports


def parse_dylink_section(wasm_file):
  module = Module(wasm_file)
  return module.parse_dylink_section()


def get_exports(wasm_file):
  module = Module(wasm_file)
  return module.get_exports()


def get_imports(wasm_file):
  module = Module(wasm_file)
  return module.get_imports()
