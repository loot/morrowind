#!/usr/bin/env python
# /// script
# requires-python = ">=3"
# dependencies = ["acefile==0.6.13"]
# ///

# https://modlist.altervista.org/mmh/index.php allows you to download a CSV of
# all listed mods. This script takes that CSV file and a directory of mod file
# downloads. Each file must have a name that's equal to the relevant mod ID in
# the CSV file. This script will read the files, looking inside archives to
# extract plugin names, and write a tab-separated file that maps plugins to
# mods.
#
# The script expects 7-zip to be installed and for the 7z executable to be
# accessible on the PATH.
#
# This script will also work with other input CSV/TSV files and downloads
# directories, so long as the file has Id, Name, Site and Link columns. However,
# the script has:
#
# - special handling for transforming the input file's URLs when the Site cell's
#   value is 'GHF', 'Fliggerty' or 'MMH'
# - hardcoded file type overrides for the download filenames 2273, 4246, 4247,
#   8202, 10811, 12076, 12972, 13138 and 13375
# - special handling for the download filenames 2770.7z.001, 2770.7z.002 and
#   2770.7z.003
#
# The output TSV file has fileName, modId, url, modName and site columns. The
# modId, modName and site values are unchanged from the input Id, Name and Site
# columns respectively.

import argparse
from collections import Counter
import csv
from enum import Enum
import gzip
import logging
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import NamedTuple

import acefile

MMH_REGEX = re.compile(r'\d{1,3}-\d{1,5}')
PLUGIN_EXTENSION_REGEX = re.compile(r'\.es[p|m]', re.IGNORECASE)

class Mod(NamedTuple):
    id: int
    name: str
    site: str
    link: str
    mod_url: str

class ModPlugin(NamedTuple):
    mod_id: str
    mod_name: str
    site: str
    mod_url: str
    plugin_name: str

class MagicBytes(NamedTuple):
    offset: int
    magic: bytes

class FileType(Enum):
    SEVENZIP = MagicBytes(0, b'\x37\x7A\xBC\xAF\x27\x1C')
    ZIP = MagicBytes(0, b'\x50\x4B\x03\x04')
    PREFIXED_ZIP = MagicBytes(0, b'\x50\x4B\x30\x30\x50\x4B\x03\x04')
    EMPTY_ZIP = MagicBytes(0, b'\x50\x4B\x05\x06')
    RAR = MagicBytes(0, b'\x52\x61\x72\x21\x1A\x07\x00')
    GZIP = MagicBytes(0, b'\x1F\x8B')
    ACE = MagicBytes(7, b'**ACE**')
    CAB = MagicBytes(0, b'MSCF')
    PDF = MagicBytes(0, b'%PDF')
    ESP = MagicBytes(0, b'TES3')
    RTF = MagicBytes(0, b'{\\rtf1')
    DDS = MagicBytes(0, b'DDS')
    NIF = MagicBytes(0, b'NetImmerse File Format')
    MZ  = MagicBytes(0, b'MZ')
    WMV = MagicBytes(0, b'\x30\x26\xB2\x75\x8E\x66\xCF\x11\xA6\xD9\x00\xAA\x00\x62\xCE\x6C')
    JIF = MagicBytes(0, b'\xFF\xD8\xFF\xDB')
    JFIF = MagicBytes(0, b'\xFF\xD8\xFF\xE0\x00\x10\x4A\x46\x49\x46\x00\x01')
    COMPOUND = MagicBytes(0, b'\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1')
    CHM = MagicBytes(0, b'\x49\x54\x53\x46\x03\x00\x00\x00\x60\x00\x00\x00')
    CPIO = MagicBytes(0, b'070707')
    HTML = 1
    TXT = 2

    def is_supported_by_7z(self):
        match self:
            case FileType.SEVENZIP | FileType.ZIP | FileType.PREFIXED_ZIP | FileType.EMPTY_ZIP | FileType.RAR | FileType.CAB | FileType.MZ | FileType.COMPOUND | FileType.CHM | FileType.CPIO:
                return True
            case _:
                # Treat GZIP as unsupported because 7z can only see the inner
                # archive, not anything inside that.
                return False

    def are_magic_bytes_present(self, buffer):
        if not isinstance(self.value, MagicBytes):
            return False

        start = self.value.offset
        end = start + len(self.value.magic)
        return buffer[start:end] == self.value.magic

FILE_TYPE_OVERRIDES = {
    # These archives are prefixed with PHP error messages.
    '13375': FileType.SEVENZIP,
    '13138': FileType.SEVENZIP,
    '12972': FileType.SEVENZIP,
    # These files are plain text files.
    '12076': FileType.TXT,
    '10811': FileType.TXT,
    '8202': FileType.TXT,
    '4247': FileType.HTML,
    '4246': FileType.TXT,
    '2273': FileType.TXT,
}

def get_altervista_url(mod_id: str) -> str:
    return f'https://modlist.altervista.org/mmh/index.php?operation=view&pk0={mod_id}'


def get_url(mod_id: str, site: str, link: str):
    match site:
        case 'GHF' | 'Fliggerty':
            return get_altervista_url(mod_id)

        case 'MMH':
            if link.startswith('https://drive.google.com/'):
                return get_altervista_url(mod_id)

            # Handle a handful of special cases. The tinyurl.com links are
            # replaced with the links that tinyurl redirects to.
            match link:
                case ('https://tinyurl.com/mwscpted'
                      | 'https://tinyurl.com/issilarMN'
                      | 'https://tinyurl.com/mwchimlight'
                      | 'https://tinyurl.com/issilar11'
                      | 'Qr4W2QrD#PgUHt_MB2iae7MosUzB_T5QkIK_5cASelSQnTddL67U'
                      | 'U6wzWTBL#7oxESyEYeCdfk4hEkjPO9BHKNKlWC673OovL-pIbgBc'
                      | 'Z2ZigRKA#JFPfvdUJG9J_iT_GMQ228qe9ksJmZgD_k0l3pAHu_Ec'):
                    return get_altervista_url(mod_id)
                case 'https://www.nexusmods.com/morrowind/mods/53730':
                    return link

            if not MMH_REGEX.fullmatch(link):
                raise RuntimeError(f'Unexpected MMH link style: {link}')

            return f'https://web.archive.org/web/20161103152243/https://mw.modhistory.com/download-{link}'

        case _:
            return link

def convert_row(row) -> Mod:
    id = row['Id']
    site = row['Site']
    link = row['Link']

    return Mod(
        id,
        row['Name'],
        site,
        link,
        get_url(id, site, link)
    )

def read_mods_csv(input, input_delimiter) -> dict[str, Mod]:
    reader = csv.DictReader(input,delimiter=input_delimiter)

    return {mod.id: mod for mod in [convert_row(row) for row in reader]}

def get_file_type(file_path: Path) -> FileType:
    if file_path.name in FILE_TYPE_OVERRIDES:
        return FILE_TYPE_OVERRIDES[file_path.name]

    # Long enough to hold the longest magic byte sequence, which is NIF's.
    BUFFER_SIZE = 22

    with open(file_path, 'rb') as input:
        buffer = input.read(BUFFER_SIZE)

        for file_type in FileType:
            if isinstance(file_type.value, MagicBytes):
                if file_type.are_magic_bytes_present(buffer):
                    return file_type

    raise RuntimeError(f'Unknown file type for: {file_path}')

def list_archive_plugins(archive_path) -> list[str]:
    # Don't check the return code because it can be non-zero due to archive read
    # errors that don't prevent at least some useful information being printed.
    result = subprocess.run(
        ['7z', 'l', '-ba', '-sccUTF-8', archive_path],
        capture_output=True,
        text=True,
        encoding='utf8'
    )

    paths = [Path(l[53:]) for l in result.stdout[:-1].split('\n')]

    # Error if there are no paths, as that suggests something went wrong.
    if not paths:
        raise RuntimeError(f'Failed to read any paths in the archive at {archive_path}')

    return [p.name for p in paths if PLUGIN_EXTENSION_REGEX.fullmatch(p.suffix)]

def list_ace_archive_plugins(archive_path: Path) -> list[str]:
    with acefile.open(str(archive_path)) as archive:
        return [f.name for f in (Path(m.filename) for m in archive if m.is_reg()) if PLUGIN_EXTENSION_REGEX.fullmatch(f.suffix)]

def list_gzip_archive_plugins(archive_path: Path) -> list[str]:
    with gzip.open(archive_path, 'rb') as archive:
        decompressed = archive.read()

        for file_type in FileType:
            if file_type.is_supported_by_7z() and isinstance(file_type.value, MagicBytes):
                if file_type.are_magic_bytes_present(decompressed):
                    # Write out a temporary file to call 7-zip on.
                    with tempfile.NamedTemporaryFile(delete_on_close=False) as file:
                        file.write(decompressed)
                        file.close()

                        return list_archive_plugins(file.name)

        raise RuntimeError(f'Unsupported content in gzip file at {archive_path}: {decompressed}')

def list_plugins(file_path: Path, file_type: FileType) -> list[str]:
    if file_type == FileType.ACE:
        return list_ace_archive_plugins(file_path)
    elif file_type == FileType.GZIP:
        return list_gzip_archive_plugins(file_path)
    elif file_type == FileType.ESP:
        raise RuntimeError(f'Unsupported ESP file for {file_path}: put this in a zip file to preserve its filename')
    elif file_type.is_supported_by_7z():
        return list_archive_plugins(file_path)
    else:
        return []

def write_to_tsv(output_path: str, mod_plugins: list[ModPlugin]):
    with open(output_path, 'w', newline='', encoding='utf8') as tsv_file:
        field_names = ['fileName', 'modId', 'url', 'modName', 'site']
        writer = csv.DictWriter(tsv_file, delimiter='\t', fieldnames=field_names)

        writer.writeheader()
        for m in mod_plugins:
            writer.writerow({
                'fileName': m.plugin_name,
                'modId': m.mod_id,
                'url': m.mod_url,
                'modName': m.mod_name,
                'site': m.site
            })

def get_sort_key(mod_plugin: ModPlugin):
    return (mod_plugin.plugin_name, int(mod_plugin.mod_id) if mod_plugin.mod_id.isdigit() else mod_plugin.mod_id)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('-i', '--index-path', default=Path.cwd() / 'data' / 'MMH_&_Fliggerty_Mods.csv')
    parser.add_argument('--index-delimiter', default=',')
    parser.add_argument('-d', '--downloads-path', default=Path.home() / "Downloads" / 'mmh_fliggerty_mods')
    parser.add_argument('-o', '--output-path', default=Path.cwd() / 'data' / 'mmh_fliggerty_plugins.tsv')
    parser.add_argument('-l', '--log-path', default=Path(__file__).with_suffix('.log'))
    parser.add_argument('-s', '--log-severity', default='info', choices=['debug', 'info', 'warning', 'error'])
    args = parser.parse_args()

    logging.basicConfig(filename=args.log_path, filemode='w', level=args.log_severity.upper())
    logging.getLogger().addHandler(logging.StreamHandler())

    mods = []
    with open(args.index_path, encoding='utf8') as input:
        mods = read_mods_csv(input, args.index_delimiter)

    file_types = Counter()
    mod_plugins = []

    for entry in os.scandir(args.downloads_path):
        try:
            if not entry.is_file():
                continue

            logging.debug(f'Processing mod archive at {entry.path}')

            file_path = Path(entry.path)
            mod_id = entry.name

            # Hack for the multi-part archive for Wizards Islands downloaded
            # from the Wayback Machine's archive of mw.modhistory.com.
            if mod_id == '2770.7z.001':
                mod_id = '2770'
            elif mod_id in ['2770.7z.002', '2770.7z.003']:
                continue

            if mod_id not in mods:
                raise RuntimeError(f'Could not find mod ID {mod_id} in mod index')

            file_type = get_file_type(file_path)

            file_types[file_type.name.lower()] += 1

            plugins = list_plugins(file_path, file_type)

            mod = mods[mod_id]
            mod_plugins.extend(ModPlugin(mod.id, mod.name, mod.site, mod.mod_url, p) for p in plugins)
        except Exception as e:
            logging.error(f'Caught exception when processing mod archive at {entry.path}: {e}')

    logging.info(f'File type counts: {file_types}')

    mod_plugins.sort(key=get_sort_key)

    write_to_tsv(args.output_path, mod_plugins)
