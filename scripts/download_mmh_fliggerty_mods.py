#!/usr/bin/env python

# https://modlist.altervista.org/mmh/index.php allows you to download a CSV of
# all listed mods, which includes URLs or URL suffixes for various mod page or
# download URLs. They are mostly usable on web.archive.org, though there are
# many mega.nz suffixes, and also a few Google Drive and Nexus Mods URLs. This
# script takes that CSV file as an input and downloads the mods listed in the
# CSV.
#
# This script expects <https://github.com/meganz/megacmd> to be installed, and
# currently hardcodes its path as the default on Windows
# (~\AppData\Local\MEGAcmd).
#
# mega.nz has a bandwidth cap that is exceeded while dowloading the files hosted
# there - when it's hit, the mega CLI will exit with code 11 and you'll need to
# wait some hours before retrying (you can manually run the CLI to see an error
# message that gives how long to wait).

import argparse
import csv
import logging
import os
from pathlib import Path
import re
import subprocess
from time import sleep
from typing import NamedTuple
from urllib.request import urlopen
import zipfile

FLIGGERTY_REGEX = re.compile(r'\*\d+')
MMH_REGEX = re.compile(r'\d{1,3}-\d{1,5}')
GOOGLE_DRIVE_URL_REGEX = re.compile(r'https://drive.google.com/file/d/([^/]+)(?:/.+)?', re.IGNORECASE)
CONTENT_DISPOSITION_REGEX = re.compile('attachment; filename="([^"]+)"')
INTERNET_ARCHIVE_SECS_BETWEEN_REQS = 4 # Apparently there's a Wayback Machine rate limit of 15 requests per minute.
MEGA_DOWNLOAD_TIMEOUT_SECS = 30
MOD_IDS_WITH_INVALID_DOWNLOADS = [
    # These are mega.nz downloads that fail due to invalid decryption keys.
    15156, 14904, 14712, 14689, 14589,
    # These are Fliggerty or Morrowind Modding History mods that point to the
    # Wayback Machine, but it responds with 404s.
    13520, 13293, 13166, 13154, 13147, 13145, 13132, 13018, 13014, 13001, 11702,
    11701, 11700, 11699, 11698, 11697, 11696, 11695, 11694, 11693, 11692, 11691,
    11690, 11689, 11688, 11687, 11686, 11685, 11582, 11581, 11580, 11287, 11260,
    11102, 11002, 10934, 10885, 10766, 10647, 10603, 10565, 10376, 10354, 10333,
    10126, 9972, 9971, 9948, 9922, 9920, 9865, 9815, 9803, 9801, 9797, 9760,
    9728, 9602, 9554, 9543, 9539, 9157, 9086, 8987, 8877, 8834, 8489, 8488,
    8471, 8312, 8272, 8091, 8008, 7875, 7850, 7654, 7618, 7612, 7579, 7534,
    7508, 7423, 7391, 7292, 7274, 7272, 7262, 7215, 7012, 6920, 6898, 6878,
    6752, 6650, 6514, 6438, 6430, 6223, 6194, 6172, 6171, 6121, 6038, 6022,
    5920, 5913, 5909, 5880, 5879, 5878, 5819, 5697, 5686, 5537, 5525, 5508,
    5494, 5363, 5346, 5174, 5049, 5046, 4678, 4672, 4574, 4469, 4463, 4456,
    4407, 4311, 4281, 4278, 4254, 3982, 3906, 3795, 3774, 3716, 3692, 3606,
    3595, 3576, 3495, 3459, 3414, 3345, 2989, 2959, 2947, 2781, 2780, 2779,
    2778, 2761, 2559, 2282, 2020, 2015, 1929, 1752, 1740, 1725, 1620, 1522,
    1520, 1334, 1295, 1278, 1241, 1194, 1108, 1094, 1085, 843, 734, 590, 468,
    466, 460, 413, 361, 310, 281, 249, 243, 239, 238, 175, 161, 83, 70, 69, 26,
    # The following don't 404, but the downloads are actually HTML pages from
    # other websites (commonly FilePlanet), even though the response headers
    # tell browsers to treat them as downloads, and in some cases suggest
    # archive filenames. In many cases the HTML pages are 404 pages from those
    # other websites, or are pages that weren't archived by the Wayback Machine,
    # but in all cases the actual mods weren't archived.
    12418, 11604, 11452, 10241, 9938, 9448, 9168, 9165, 9011, 8828, 8726, 8713,
    8461, 8295, 8104, 7737, 5786, 5400, 5007, 4852, 3967, 3397, 2985, 2838,
    2707, 2705, 2362, 2195, 2132, 1935, 1880, 1851, 1421,
    # These redirect to a generic status page.
    5930,
    # These downloaded files are empty
    9664, 8299,
    # These downloaded files are invalid RAR files
    9164, 7873
]

class ModUrls(NamedTuple):
    mod: str
    download: str

class Mod(NamedTuple):
    id: int
    mod_url: str
    download_url: str

def get_urls(site, link):
    match site:
        case 'GHF':
            if '#' not in link:
                raise RuntimeError(f'Unexpected GHF link style: {link}')

            return ModUrls(None, f'https://mega.nz/file/{link}')

        case 'Fliggerty':
            if link.startswith('https://drive.google.com/'):
                return ModUrls(None, link)

            if not FLIGGERTY_REGEX.fullmatch(link):
                raise RuntimeError(f'Unexpected Fliggerty link style: {link}')

            return ModUrls(None, f'https://web.archive.org/web/20161103125749/https://download.fliggerty.com/file.php?id={link[1:]}')

        case 'MMH':
            if link.startswith('https://drive.google.com/'):
                return ModUrls(None, link)

            # Handle a handful of special cases. The tinyurl.com links are
            # replaced with the links that tinyurl redirects to.
            match link:
                case 'https://tinyurl.com/mwscpted':
                    return ModUrls(None, 'https://drive.google.com/file/d/10GHbDtL8msJOlzrIiAREUpjhY5Vi4F8K')
                case 'https://tinyurl.com/issilarMN':
                    return ModUrls(None, 'https://drive.google.com/file/d/1-Gxg2LLus0DcMZ_cV44weKtdpMdJ0I1Z')
                case 'https://tinyurl.com/mwchimlight':
                    return ModUrls(None, 'https://drive.google.com/file/d/1qczqgvCgB4q5QrsdN3hbGx525ho4UBRP')
                case 'https://tinyurl.com/issilar11':
                    return ModUrls(None, 'https://drive.google.com/file/d/1VYTls5ahO-H3JVCL3wsfMpExwgCImlBZ')
                case 'Qr4W2QrD#PgUHt_MB2iae7MosUzB_T5QkIK_5cASelSQnTddL67U' | 'U6wzWTBL#7oxESyEYeCdfk4hEkjPO9BHKNKlWC673OovL-pIbgBc' | 'Z2ZigRKA#JFPfvdUJG9J_iT_GMQ228qe9ksJmZgD_k0l3pAHu_Ec':
                    return ModUrls(None, f'https://mega.nz/file/{link}')
                case 'https://www.nexusmods.com/morrowind/mods/53730':
                    return ModUrls(link, None)

            if not MMH_REGEX.fullmatch(link):
                raise RuntimeError(f'Unexpected MMH link style: {link}')

            return ModUrls(
                f'https://web.archive.org/web/20161103152243/https://mw.modhistory.com/download-{link}',
                f'https://web.archive.org/web/20161103152243/https://mw.modhistory.com/file.php?id={link.split('-')[1]}'
            )

        case 'Nexus':
            if not link.startswith('https://www.nexusmods.com/'):
                raise RuntimeError(f'Unexpected Nexus link style: {link}')

            return ModUrls(link, None)
        case _:
            raise RuntimeError(f'Unexpected site: {site}')

def convert_row(row) -> Mod:
    urls = get_urls(row['Site'], row['Link'])

    return Mod(
        int(row['Id']),
        urls.mod,
        urls.download
    )

def read_mods_csv(input):
    reader = csv.DictReader(input)

    return [convert_row(row) for row in reader]

def download_from_google_drive(download_url: str, output_file_path: Path):
    match = GOOGLE_DRIVE_URL_REGEX.fullmatch(download_url)
    if not match:
        raise RuntimeError(f'Google drive URL "{download_url}" did not match expected regex')

    id = match.group(1)

    download_url = f'https://drive.usercontent.google.com/download?id={id}&export=download'

    response = urlopen(download_url)
    if response.status != 200:
        raise RuntimeError(f'Unexpected response status code: {response.status}')

    with open(output_file_path, 'wb') as file:
        file.write(response.read())

def download_from_mega(download_url: str, output_file_path: Path):
    # This assumes you're running on Windows and have the Mega CLI installed in its default location.
    mega_get_path = Path.home() / 'AppData' / 'Local' / 'MEGAcmd' / 'mega-get.bat'

    subprocess.run(
        [mega_get_path, download_url, output_file_path],
        capture_output=True,
        text=True,
        check=True,
        timeout=MEGA_DOWNLOAD_TIMEOUT_SECS
    )

def download_from_wayback_machine(download_url: str, output_file_path: Path) -> str | None:
    # Respect the Internet Archive's, which is enforced by closing connections rather than HTTP error responses.
    sleep(INTERNET_ARCHIVE_SECS_BETWEEN_REQS)
    response = urlopen(download_url)
    if response.status != 200:
        raise RuntimeError(f'Unexpected response status code: {response.status}')

    # The URL gets redirected to a specific page, and that page's URL can be used to get the real download URL.
    url = response.url.replace('/http://', 'if_/http://').replace('/https://', 'if_/https://')

    sleep(INTERNET_ARCHIVE_SECS_BETWEEN_REQS)
    response = urlopen(url)
    if response.status != 200:
        raise RuntimeError(f'Unexpected response status code: {response.status}')

    with open(output_file_path, 'wb') as file:
        file.write(response.read())

    if 'content-disposition' in response.headers:
        match = CONTENT_DISPOSITION_REGEX.fullmatch(response.headers['content-disposition'])
        if match:
            return match.group(1)
        else:
            logging.error(f'Failed to match regex for content-disposition header {response.headers['content-disposition']}')

    return None

def download_mod(mod: Mod, output_file_path: Path):
    logging.info(f'Downloading mod with ID {mod.id} from URL {mod.download_url}')

    if mod.download_url.startswith('https://drive.google.com/'):
        download_from_google_drive(mod.download_url, output_file_path)
        return None

    if mod.download_url.startswith('https://mega.nz/'):
        download_from_mega(mod.download_url, output_file_path)
        return None

    if mod.download_url.startswith('https://web.archive.org/'):
        return download_from_wayback_machine(mod.download_url, output_file_path)

    raise RuntimeError(f'Unexpected URL: {mod.download_url}')

def is_plugin_file(file_path: Path) -> bool:
    with open(file_path, 'rb') as input:
        return input.read(4) == b'TES3'

def plugin_to_zip(file_path: Path, plugin_name: str):
    logging.info(f'Turning mod ID {mod.id}\'s downloaded plugin file into a zip file containing a file named {plugin_name}')

    plugin_path = file_path.with_name(plugin_name)
    os.rename(file_path, plugin_path)

    with zipfile.ZipFile(file_path, mode='x') as zip:
        zip.write(plugin_path, plugin_name)

    os.remove(plugin_path)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('-i', '--input-path', default=Path.cwd() / 'data' / 'MMH_&_Fliggerty_Mods.csv')
    parser.add_argument('-d', '--downloads-path', default=Path.home() / "Downloads" / 'mmh_fliggerty_mods')
    parser.add_argument('-l', '--log-path', default=Path(__file__).with_suffix('.log'))
    parser.add_argument('-s', '--log-severity', default='info', choices=['debug', 'info', 'warning', 'error'])
    args = parser.parse_args()

    logging.basicConfig(filename=args.log_path, filemode='w', level=args.log_severity.upper())
    logging.getLogger().addHandler(logging.StreamHandler())

    mods = []
    with open(args.input_path, encoding='utf8') as input:
        mods = read_mods_csv(input)

    downloads_path = Path(args.downloads_path)
    downloads_path.mkdir(parents=True, exist_ok=True)

    for index, mod in enumerate(mods):
        try:
            logging.debug(f'Processing mod {index+1} of {len(mods)}...')

            if mod.id in MOD_IDS_WITH_INVALID_DOWNLOADS:
                logging.debug(f'Skipping mod ID {mod.id} because its download is known to fail or be invalid')
                continue

            if not mod.download_url:
                logging.warning(f'Cannot download mod with ID {mod.id} because it has no direct download URL: it will need to be manually downloaded from its page at {mod.mod_url}')
                continue

            output_file_path = downloads_path / str(mod.id)

            # Hack for a multi-part archive that's split across 3 mod entries.
            if mod.id == 2770:
                output_file_path = downloads_path / '2770.7z.001'
            elif mod.id == 2771:
                output_file_path = downloads_path / '2770.7z.002'
            elif mod.id == 2772:
                output_file_path = downloads_path / '2770.7z.003'

            if not output_file_path.exists():
                suggested_filename = download_mod(mod, output_file_path)

                if is_plugin_file(output_file_path):
                    # Some downloads are just plugin files, to preserve the plugin
                    # filenames turn them into zip files that contain the plugin.
                    if not suggested_filename:
                        raise RuntimeError(f'Could not get name of plugin download for mod {mod.id}')

                    plugin_to_zip(output_file_path, suggested_filename)

        except Exception as e:
            logging.error(f'Caught exception when processing mod ID {mod.id}: {e}')
