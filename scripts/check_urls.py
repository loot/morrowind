#!/usr/bin/env python
# /// script
# requires-python = ">=3"
# dependencies = ["pyyaml==6.0.2"]
# ///

# This script checks the validity of URLs present in a given masterlist.
#
# URLs are extracted from:
#
# - message content strings (including strings in multilingual content)
# - message subs
# - file detail values (including multilingual values)
# - location url values
#
# The results of checks are written out to a tab-separated file that gives the
# URL, its status, and (optionally) a replacement URL and source for that URL,
# and an optional comment.
#
# A file of that structure can also be passed as input, in which case URLs that
# have the existing statuses DEFUNCT_NOT_ARCHIVED, DEFUNCT_VERIFIED and
# VALID_VERIFIED will have those preserved, and replacement URLs and
# their sources will be preserved when the source is REQUEST or MANUAL. Comments
# are also preserved for all URLs.
#
# It's also possible to supply plugin index files: if a URL is present in any of
# the loaded index files, it's given the VALID_FROM_INDEX status (unless another
# existing status is preserved). The index will also be used to look for
# replacement Wayback Machine URLs for URLs that the script gives the
# DEFUNCT_HOSTNAME status to: those replacement URLs will be given the INDEX
# source.
#
# Replacement URLs are also derived for URLs that have the VALID_FROM_INDEX or
# VALID_VERIFIED statuses: this does things like turn HTTP into HTTPS
# for hostnames that are known to support it. Such replacement URLs are given
# the source DERIVED.
#
# Before trying to query a URL, the URL's hostname is checked against three
# hardcoded lists of hostnames: one that contains those known to be dangerous
# (e.g. because they host malware), one of those known to be defunct, and one
# of those that are known to block the script's automated queries. If a match
# is found in one of those lists, the URL is given the status of DANGEROUS,
# DEFUNCT_HOSTNAME or SKIPPED_BLOCKED respectively.
#
# If a URL does not have an existing preserved state and is not given a state
# for any other reason, the script queries the URL and gives it a status of
# SUCCESS_RESPONSE, ERROR_NOT_FOUND or ERROR_UNKNOWN, depending on the response.
# If the status is SUCCESS_RESPONSE and the request was redirected to a
# different URL, the final URL is recorded as the replacement URL, with the
# source REQUEST.
#
# If -w or --with-wayback-check is passed, then if a URL is DEFUNCT_HOSTNAME and
# doesn't already have a replacement URL for another reason, or if a URL is
# ERROR_NOT_FOUND, then the script will also check if the URL is archived in the
# Wayback Machine. If it is, that URL is recorded as the replacement URL, with
# the source REQUEST. If the Wayback Machine responds with a 404 because the
# URL is not archived, the URL's status is changed to DEFUNCT_NOT_ARCHIVED. If
# the Wayback Machine responds with a 404 because it archived a 404 page, the
# status is changed to DEFUNCT_ARCHIVED_404.
#
# Manual post-processing suggestions:
#
# - SUCCESS_RESPONSE URLs should be checked to see if they're valid and updated
#   to be DEFUNCT_VERIFIED or VALID_VERIFIED as appropriate.
# - ERROR_UNKNOWN and ERROR_NOT_FOUND URLs should be checked and updated to be
#   DEFUNCT_VERIFIED if appropriate.
# - DEFUNCT_VERIFIED URLs may not have been been checked for in the Wayback
#   Machine, so you could check them and update them to DEFUNCT_ARCHIVED_404 or
#   DEFUNCT_NOT_ARCHIVED if appropriate.
# - SKIPPED_BLOCKED URLs should be checked in a web browser and their status
#   updated to DEFUNCT_VERIFIED or VALID_VERIFIED as appropriate.
# - The content that used to be accessible at DEFUNCT_NOT_ARCHIVED URLs may be
#   archived on the Wayback Machine under slightly different URLs, so if a
#   URL has a hash or query parameters, you could try removing or changing them
#   in case it was possible to navigate to the same content in a different way.
# - DEFUNCT_ARCHIVED_404 URLs may have valid older versions archived, or may
#   have their content accessible under different URLs, like
#   DEFUNCT_NOT_ARCHIVED URLs, so you could check for both cases.
# - Replacement URLs with the REQUEST status should be checked and updated to
#   REQUEST_VERIFIED if appropriate.
#
# If when manually checking a URL it turns out to be dangerous, add its hostname
# to this script's DANGEROUS_HOSTNAMES list. Similarly, the DEFUNCT_HOSTNAMES
# can be updated if an entire site is no longer accessible, and
# BLOCKED_HOSTNAMES can be updated for sites that consistently work in a browser
# but not in this script.

import argparse
import csv
from enum import Enum, auto
import logging
from pathlib import Path
import re
from time import sleep
from typing import NamedTuple
from urllib.parse import urlparse
import urllib.request

import yaml

class UrlStatus(Enum):
    # A URL that was not checked in any way.
    UNCHECKED = auto()

    # A URL that couldn't be opened for an unknown (i.e. unrecognised by this
    # script) reason.
    ERROR_UNKNOWN = auto()

    # A URL that caused a 404 response when a request was sent to it.
    ERROR_NOT_FOUND = auto()

    # A URL that caused a 2xx response (potentially after redirects) when a
    # request was sent to it. The content in the response may not have been
    # relevant.
    SUCCESS_RESPONSE = auto()

    # A URL that has a hostname that is known to serve unsafe content (e.g.
    # malware, including unexpected redirects to advertising spam or porn).
    DANGEROUS_HOSTNAME = auto()

    # A URL that can't be checked by this script because it's blocked by the
    # host. It can be manually checked in a web browser.
    SKIPPED_BLOCKED = auto()

    # A URL with a hostname that is known to be unreachable or unresponsive, or
    # that no longer contains any relevant content.
    DEFUNCT_HOSTNAME = auto()

    # A URL that either has a known defunct hostname or that caused an error
    # when requested, and that received a 404 from the Wayback Machine because
    # it has a 404 page archived. An older non-404 page may also be archived.
    # This can be inherited from the input URLs TSV or set by this script.
    DEFUNCT_ARCHIVED_404 = auto()

    # A URL that either has a known defunct hostname or that caused an error
    # when requested, and that received a 404 from the Wayback Machine because
    # the page is not archived.
    # If the URL is for a forum thread or post, or similarly contains query
    # parameters, the same content may be archived under a slightly different
    # URL. Try checking the archive for the simplest form of the URL (e.g. just
    # try to open the forum thread instead of a specific post).
    # This can be inherited from the input URLs TSV or set by this script.
    DEFUNCT_NOT_ARCHIVED = auto()

    # A URL that was already recorded with this status in the input URLs TSV.
    # Can be used for URLs that were given ERROR_UNKNOWN or ERROR_NOT_FOUND
    # statuses and then manually verified to be defunct.
    # It's never newly set by this script.
    DEFUNCT_VERIFIED = auto()

    # A URL that is treated as valid because it or an equivalent URL appears in
    # the given plugins index(es).
    VALID_FROM_INDEX = auto()

    # A URL that was already recorded with this status in the input URLs TSV.
    # Can be used for URLs that were given SUCCESS_RESPONSE status and then
    # manually verified to be valid.
    # It's never newly set by this script.
    VALID_VERIFIED = auto()

class ReplacementSource(Enum):
    # Derived from the original URL, e.g. replacing http:// with https://
    DERIVED = auto()

    # Found in the index as an equivalent, e.g. a Wayback Machine URL for a
    # defunct URL.
    INDEX = auto()

    # The final URL of a successful request, after any redirects. The page
    # may not contain any relevant content. These replacements can also be
    # inherited from the input URLs TSV, since sending requests is relatively
    # slow, but will be overridden by replacements from other sources.
    REQUEST = auto()

    # A URL that initially had the REQUEST source and was then manually verified
    # to be valid.
    # It's never newly set by this script.
    REQUEST_VERIFIED = auto()

    # The replacement was provided manually in the input URLs TSV.
    # It's never newly set by this script.
    MANUAL = auto()

# URLs that have these statuses in the input URLs TSV have their statuses
# retained.
INHERITABLE_STATUSES = set([
    UrlStatus.DEFUNCT_NOT_ARCHIVED,
    UrlStatus.DEFUNCT_ARCHIVED_404,
    UrlStatus.DEFUNCT_VERIFIED,
    UrlStatus.VALID_VERIFIED,
])

# URLs that have these statuses in the input URLs TSV have their replacement
# URLs and replacement URL sources retained.
INHERITABLE_SOURCES = set([
    ReplacementSource.REQUEST,
    ReplacementSource.REQUEST_VERIFIED,
    ReplacementSource.MANUAL,
])

class CheckedUrl(NamedTuple):
    url: str
    status: UrlStatus
    replacement_url: str | None
    replacement_source: ReplacementSource
    comment: str | None

COMMENT_DNS = 'The DNS query errors.'
COMMENT_TIMEOUT = 'The request times out.'
COMMENT_WEB_HOST_REDIRECT = 'Redirects to a hosting platform page saying the site does not exist.'

DEFUNCT_HOSTNAMES = {
    'arcimaestroantares.webs.com': COMMENT_DNS,
    'baratheon79.rpgmods.com': COMMENT_DNS,
    'canadianice.moddersrealm.com': COMMENT_DNS,
    'dianahliva.com': COMMENT_DNS,
    'download.fliggerty.com': COMMENT_TIMEOUT,
    'escf.rethan-manor.net': 'The server responds with a 500 error page.',
    'forums.bethsoft.com': 'Redirects to bethesda.net: the forums were taken offline in 2020.',
    'gmml.pbwiki.com': COMMENT_WEB_HOST_REDIRECT,
    'grotto.moddersrealm.com': COMMENT_DNS,
    'home.earthlink.net': COMMENT_DNS,
    'morrgraphext.wiki.sourceforge.net': 'TLS negotation fails.',
    'mw.modhistory.com': COMMENT_TIMEOUT,
    'planetelderscrolls.gamespy.com': 'The server responds with a 403 error page.',
    'static-2.nexusmods.com': 'The server responds with a Cloudflare error 1016 page.',
    'webpages.charter.net': COMMENT_TIMEOUT,
    'wolflore.net': COMMENT_TIMEOUT,
    'www.angelfire.com': COMMENT_TIMEOUT,
    'www.morrowind4kids.com': COMMENT_DNS,
    'www.fliggerty.com': COMMENT_TIMEOUT,
    'www.freewebs.com': COMMENT_WEB_HOST_REDIRECT,
    'www.pirates.retreat.btinternet.co.uk': COMMENT_TIMEOUT,
    'www3.telus.net': COMMENT_TIMEOUT,
}

# These hostnames consistently block requests from this script, but not from
# web browsers.
BLOCKED_HOSTNAMES = set([
    # For these three, Cloudflare responds with a 403 saying that JavaScript and
    # cookies need to be enabled.
    'morrowind.nexusmods.com',
    'www.nexusmods.com',
    'tamriel-rebuilt.org'
])

# Risky might be a more accurate term, but it's dangerous to spread risky URLs.
DANGEROUS_HOSTNAMES = set([
    # These domains each redirect to a variety of malware and advertising pages.
    'wiki.theassimilationlab.com',
    'www.theassimilationlab.com',
    'emma.ufrealms.net',
    'wrye.ufrealms.net',
    'www.elricm.com',
    'www.ladymoiraine.com',
    'www.mw.yacoby.net',
    'www.ornitocopter.net',

    # http://wolflore.net/viewtopic.php?f=15&t=2086&sid=cdfbf3957d2d88de3f2c5a4ccda2b76f
    # redirects to <www.wolflore.net/blog/>, which initially loads what looks
    # an auto-generated blog that is just filled with SEO keywords, mostly in
    # Thai, then that redirects <https://www.wolflore.net/>, which displays
    # a Cloudflare 524 error page. This might just be defunct, but it's
    # borderline.
    'wolflore.net',

    # http://www.fileplanet.com/167255/160000/fileinfo/Elder-Scrolls-III:-Morrowind---Silgrad-Tower-1-4_6-update redirects to a
    # pretty sketchy-looking site that tries to trick you into downloading
    # Opera, and then downloads a setup executable that seems suspicious.
    'www.fileplanet.com',

    # Redirects to silgrad.com, which displays one of those generic domain
    # squatting advertising pages. Seems harmless at the moment, but presumably
    # someone could buy the domain and change that.
    'cowguru.silgrad.com',
    'yacoby.silgrad.com',

    # Redirects to invisionfree.com/, which looks like an anime streaming site
    # with a dodgy modal dialog that redirects to porn when you try to dismiss
    # it (and accepting it opens an equally dodgy cam site). Clicking anywhere
    # also seems to flip a coin to decide whether to show you a porn in a modal
    # dialog.
    'z13.invisionfree.com',
])

# This is not an exhaustive list, it's just hostnames I've seen HTTP URLs for.
HOSTNAMES_SUPPORTING_HTTPS = set([
    'forums.nexusmods.com',
    'morrowind.nexusmods.com',
    'www.nexusmods.com',

    'abitoftaste.altervista.org',
    'fallingawkwardly.com',
    'lovkullen.net',
    'princessstomper.wordpress.com',
    'web.archive.org',
    'wryemusings.com',
    'www.automatichamster.com',
    'www.calislahn.com',
    'www.mediafire.com',
    'www.sabregirl.com',
    'www.sheikizza.boneflower.com'
    'www.uesp.net',
    'www.youtube.com'
])

# Prefixes to try adding to normalised URLs before looking for them in the index.
WAYBACK_URL_INDEX_PREFIXES = [
    'web.archive.org/web/20161103152243/https://',
    'web.archive.org/web/20161103125749/https://',
]

def read_urls_from_plugins_index(input) -> list[str]:
    reader = csv.DictReader(input, delimiter='\t')

    return [row['url'] for row in reader]

def read_from_tsv(input_path: Path) -> dict[str, CheckedUrl]:
    with open(input_path, 'r', encoding='utf8') as tsv_file:
        reader = csv.DictReader(tsv_file, delimiter='\t')

        return {
            row['url']: CheckedUrl(row['url'],
                                   getattr(UrlStatus, row['status']),
                                   row['replacement'],
                                   getattr(ReplacementSource, row['replacement_source']) if row['replacement_source'] else None,
                                   row['comment'])
            for row in reader
        }

def write_to_tsv(output_path: Path, url_rows: list[CheckedUrl]):
    with open(output_path, 'w', newline='', encoding='utf8') as tsv_file:
        field_names = ['url', 'status', 'replacement', 'replacement_source', 'comment']
        writer = csv.DictWriter(tsv_file, delimiter='\t', fieldnames=field_names)

        writer.writeheader()
        for row in url_rows:
            writer.writerow({
                'url': row.url,
                'status': row.status.name,
                'replacement': row.replacement_url,
                'replacement_source': row.replacement_source.name if row.replacement_source else None,
                'comment': row.comment
            })

def normalise_url(url: str) -> str:
    if url.startswith('https://'):
        url = url[8:]
    elif url.startswith('http://'):
        url = url[7:]
    else:
        raise RuntimeError(f'Unexpected URL scheme: {url}')

    if url[-1] == '/':
        url = url[:-1]

    prefix = 'morrowind.nexusmods.com/'
    if url.startswith(prefix):
        url = 'www.nexusmods.com/morrowind/' + url[len(prefix):]

    return url

def extract_urls_from_message_contents(contents):
    if isinstance(contents, str):
        return set(extract_urls_from_string(contents))
    else:
        urls = set()
        for content in contents:
            urls.update(extract_urls_from_string(content['text']))

        return urls

def extract_urls_from_message(message):
    urls = extract_urls_from_message_contents(message['content'])

    if 'subs' in message:
        for sub in message['subs']:
            urls.update(extract_urls_from_string(sub))

    return urls

def extract_urls_from_location(location):
    if isinstance(location, str):
        return set([location])
    else:
        return set([location['link']])

def extract_urls_from_string(value: str):
    URL_REGEX = re.compile('(https?://[^\\s)]+)')

    return re.findall(URL_REGEX, value)


def get_urls(masterlist):
    urls = set()

    if 'globals' in masterlist:
        for message in masterlist['globals']:
            urls.update(extract_urls_from_message(message))

    if 'plugins' in masterlist:
        for plugin in masterlist['plugins']:
            if 'msg' in plugin:
                for message in plugin['msg']:
                    urls.update(extract_urls_from_message(message))

            if 'req' in plugin:
                for file in plugin['req']:
                    if 'detail' in file:
                        urls.update(extract_urls_from_message_contents(file['detail']))

            if 'inc' in plugin:
                for file in plugin['inc']:
                    if 'detail' in file:
                        urls.update(extract_urls_from_message_contents(file['detail']))

            if 'url' in plugin:
                for location in plugin['url']:
                        urls.update(extract_urls_from_location(location))

    return urls

def offline_check_url(url: str) -> tuple[UrlStatus, str | None] | None:
    parsed_url = urlparse(url)

    if parsed_url.hostname in DANGEROUS_HOSTNAMES:
        logging.warning(f'The URL {url} has a hostname that is known to be dangerous')
        return (UrlStatus.DANGEROUS_HOSTNAME, None)

    if parsed_url.hostname in DEFUNCT_HOSTNAMES:
        logging.info(f'The URL {url} has a hostname that is known to be defunct')
        return (UrlStatus.DEFUNCT_HOSTNAME, DEFUNCT_HOSTNAMES[parsed_url.hostname])

    if parsed_url.hostname in BLOCKED_HOSTNAMES:
        logging.warning(f'Skipping the URL {url} because {parsed_url.hostname} is known to block automated access: this URL will need to be checked manually')
        return (UrlStatus.SKIPPED_BLOCKED, None)

    return None

def rewrite_url(url: str) -> str | None:
    NEXUS_MOD_REGEX = re.compile(r'https?://(?:morrowind.nexusmods.com|www.nexusmods.com/morrowind)/mods/(\d+)/?(\?.*)?')

    match = NEXUS_MOD_REGEX.fullmatch(url)
    rewritten = None
    if match:
        if match[2] and match[2] not in ['?', '?tab=description']:
            rewritten = f'https://www.nexusmods.com/morrowind/mods/{match.group(1)}{match.group(2)}'
        else:
            rewritten = f'https://www.nexusmods.com/morrowind/mods/{match.group(1)}'

    elif url.startswith('http://') and urlparse(url).hostname in HOSTNAMES_SUPPORTING_HTTPS:
        rewritten = f'https:{url[5:]}'

    if rewritten and rewritten != url:
        return rewritten
    else:
        return None

def send_request_shared(url: str, timeout) -> tuple[str, UrlStatus]:
    try:
        request = urllib.request.Request(url, headers={
            # If no user agent is provided, forums.nexusmods.com responds with 403s.
            'User-Agent': 'check_urls.py'
        })
        response = urllib.request.urlopen(request, timeout=timeout)
        logging.debug(f'Successfully opened the URL <{url}>. This does not mean it is valid: the content at that URL may have been replaced with other content that is irrelevant or dangerous.')

        if response.url and response.url != url:
            return (response.url, UrlStatus.SUCCESS_RESPONSE)
        else:
            return (None, UrlStatus.SUCCESS_RESPONSE)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # Do more specific handling higher up.
            raise e
        else:
            logging.warning(f'The URL {url} could not be opened: {e}')
            return (None, UrlStatus.ERROR_UNKNOWN)
    except Exception as e:
        logging.warning(f'The URL {url} could not be opened: {e}')
        return (None, UrlStatus.ERROR_UNKNOWN)

def send_request(url: str) -> tuple[str, UrlStatus]:
    try:
        return send_request_shared(url, 2)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            logging.warning(f'The URL {url} received a 404 response.')
            return (None, UrlStatus.ERROR_NOT_FOUND)
        else:
            return (None, UrlStatus.ERROR_UNKNOWN)

def send_wayback_request(url: str) -> tuple[str, UrlStatus]:
    try:
        # Respect the Internet Archive's rate limit, which is enforced by
        # closing connections rather than HTTP error responses.
        sleep(10)

        wayback_url = f'https://web.archive.org/web/{url}'

        return send_request_shared(wayback_url, 30)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # If the Wayback Machine hasn't archived the page, it returns 404,
            # but it can also return 404 if it archived a 404 page, so check
            # the response headers to tell which it is.
            if 'x-archive-orig-date' in e.headers:
                logging.warning(f'The URL {url} is most recently archived on the Wayback Machine as a 404 page.')
                status = UrlStatus.DEFUNCT_ARCHIVED_404
            else:
                logging.warning(f'The URL {url} is not archived on the Wayback Machine.')
                status = UrlStatus.DEFUNCT_NOT_ARCHIVED

            return (None, status)
        else:
            return (None, UrlStatus.ERROR_UNKNOWN)

def check_urls(urls: set[str], known_valid_urls: list[str], existing_checked_urls: dict[str, UrlStatus], with_wayback_check: bool):
    index_urls = {normalise_url(u): u for u in known_valid_urls}

    url_rows = []
    for index, original_url in enumerate(sorted(urls)):
        # Un-escape anything in URLs that has been escaped for CommonMark.
        original_url = original_url.replace('\\', '')
        status = UrlStatus.UNCHECKED
        replacement_url = None
        replacement_source = None
        comment = None

        normalised_url = None

        logging.debug(f'Checking url {index+1} of {len(urls)}: {original_url}')

        if original_url in existing_checked_urls:
            existing_check = existing_checked_urls[original_url]

            if existing_check.status in INHERITABLE_STATUSES:
                status = existing_check.status

            if existing_check.replacement_source in INHERITABLE_SOURCES:
                replacement_url = existing_check.replacement_url
                replacement_source = existing_check.replacement_source

            comment = existing_check.comment

        if status == UrlStatus.UNCHECKED:
            normalised_url = normalise_url(original_url)

            if normalised_url in index_urls:
                logging.debug(f'Skipping the URL {original_url} because it appears in the plugins index, so it is assumed to be valid')
                status = UrlStatus.VALID_FROM_INDEX
            else:
                outcome = offline_check_url(original_url)
                if outcome:
                    status = outcome[0]
                    comment = outcome[1]

        # Only suggest an generically-derived replacement URL if the status is valid.
        if status in [UrlStatus.VALID_FROM_INDEX, UrlStatus.VALID_VERIFIED]:
            replacement_url = rewrite_url(original_url)
            if replacement_url:
                replacement_source = ReplacementSource.DERIVED

        elif status == UrlStatus.DEFUNCT_HOSTNAME:
            for prefix in WAYBACK_URL_INDEX_PREFIXES:
                wayback_url = f'{prefix}{normalised_url}'
                if wayback_url in index_urls:
                    logging.debug(f'The URL {original_url} is defunct, but there is an equivalent Wayback Machine URL in the index')
                    replacement_url = f'https://{wayback_url}'
                    replacement_source = ReplacementSource.INDEX
                    break

        if status == UrlStatus.UNCHECKED:
            replacement_url, status = send_request(original_url)
            if replacement_url:
                replacement_source = ReplacementSource.REQUEST

        if with_wayback_check and (
            (status == UrlStatus.DEFUNCT_HOSTNAME and not replacement_url)
            or status == UrlStatus.ERROR_NOT_FOUND
        ):
            # web.archive.org responds with 404 if it hasn't archived a URL, so
            # we can check if the archive has anything for a given URL with a
            # defunct hostname. That doesn't mean it's correct though.
            logging.info(f'Checking to see if the Wayback Machine has archived {original_url}...')

            # Use a timeout of 30s since the archive is slow.
            replacement_url, wayback_status = send_wayback_request(original_url)

            if wayback_status in [UrlStatus.DEFUNCT_NOT_ARCHIVED, UrlStatus.DEFUNCT_ARCHIVED_404]:
                status = wayback_status

            if replacement_url:
                replacement_source = ReplacementSource.REQUEST

        url_rows.append(CheckedUrl(
            original_url,
            status,
            replacement_url,
            replacement_source,
            comment
        ))

    url_rows.sort(key=lambda r: (r.status.value, r.url, r.replacement_url, r.replacement_source, r.comment))

    return url_rows

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('-i', '--input-paths', action='append', default=[Path.cwd() / 'masterlist.yaml'])
    parser.add_argument('-u', '--urls-tsv')
    parser.add_argument('-o', '--output-path', default=Path.cwd() / 'data' / 'urls.tsv')
    parser.add_argument('-l', '--log-path', default=Path(__file__).with_suffix('.log'))
    parser.add_argument('-s', '--log-severity', default='info', choices=['debug', 'info', 'warning', 'error'])
    parser.add_argument('-p', '--plugins-index-paths', action='append', default=[])
    parser.add_argument('-w', '--with-wayback-check', action=argparse.BooleanOptionalAction)
    args = parser.parse_args()

    logging.basicConfig(filename=args.log_path, filemode='w', level=args.log_severity.upper())
    logging.getLogger().addHandler(logging.StreamHandler())

    urls = set()
    for input_path in args.input_paths:
        with open(input_path, encoding='utf-8') as input:
            masterlist = yaml.safe_load(input)
            urls.update(get_urls(masterlist))

    known_valid_urls = []
    for plugins_index_path in args.plugins_index_paths:
        with open(plugins_index_path, encoding='utf8') as input:
            known_valid_urls.extend(read_urls_from_plugins_index(input))

    urls_tsv_path = args.urls_tsv if args.urls_tsv else args.output_path
    existing_checked_urls = read_from_tsv(urls_tsv_path) if Path(urls_tsv_path).exists() else []

    url_rows = check_urls(urls, known_valid_urls, existing_checked_urls, args.with_wayback_check)

    counters = {s.name: 0 for s in UrlStatus}

    for row in url_rows:
        counters[row.status.name] += 1

    logging.info(f'Status counts: {counters}')

    write_to_tsv(args.output_path, url_rows)
