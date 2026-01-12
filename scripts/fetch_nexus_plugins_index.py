#!/usr/bin/env python
# /// script
# requires-python = ">=3"
# dependencies = ["gql[all]", "pyyaml==6.0.2"]
# ///
#
# This script queries the Nexus Mods GraphQL API to retrieve a list of all
# plugin files in all file uploads for all hosted mods for the given game. The
# data is written out as a tab-separated values file containing fileName, gameId
# fileId, modId and url columns.
#
# If no output file path is provided, the file will be written to
# ./data/nexus_mods_<game name>_plugins.tsv.
#
# It's also possible to provide a masterlist file path, in which case the
# queries will ask for only the plugin filenames that have non-regex metadata
# entries that do not have location metadata.

import argparse
import csv
import logging
from pathlib import Path
from time import sleep

from gql import Client, gql
from gql.transport.aiohttp import AIOHTTPTransport

import yaml

API_URL = 'https://api.nexusmods.com/v2/graphql'
COUNT_PER_PAGE = 80 # 80 is the max
SECS_BETWEEN_REQS = 2
PLUGIN_FILE_EXTENSIONS = ['.esp', '.esl', '.esm',
                          '.ESP', '.ESL', '.ESM',
                          '.Esp', '.Esl', '.Esm',
                          '.eSp', '.eSl', '.eSm',
                          '.esP', '.esL', '.esM',
                          '.ESp', '.ESl', '.ESm',
                          '.eSP', '.eSL', '.eSM',
                          '.EsP', '.EsL', '.EsM']
GAME_NAMES = [
    'Morrowind',
    'Oblivion',
    'Oblivion Remastered',
    'Skyrim',
    'Skyrim Special Edition',
    'Fallout 3',
    'Fallout New Vegas',
    'Fallout 4',
    'Starfield',
    "Nehrim: At Fate's Edge",
    'Enderal',
    'Enderal Special Edition'
]

def is_regex_plugin(plugin) -> bool:
    return any(n in plugin['name'] for n in [':', '\\', '*', '?', '|'])

def find_plugins_without_urls(masterlist_path) -> list[str]:
    with open(masterlist_path, encoding='utf8') as file:
        data = yaml.safe_load(file)

        return [p['name'] for p in data['plugins'] if 'url' not in p and not is_regex_plugin(p)]

# <https://graphql.nexusmods.com/#query-games>
def get_games(client):
    logging.info('Getting metadata for games supported by LOOT...')

    query = gql("""
        query games(
            $filter: GamesSearchFilter,
            $sort: [GamesSearchSort!],
            $offset: Int,
            $count: Int
        ) {
            games(filter: $filter, sort: $sort, offset: $offset, count: $count) {
                nodes {
                    name,
                    domainName,
                    modCount,
                    id
                },
                nodesCount,
                totalCount
            }
        }
        """)

    query.variable_values = {
        "filter": {
            "op": "OR",
            "name": [{ "op": "EQUALS", "value": g } for g in GAME_NAMES]
        },
        "sort": [
                {
                    "name": {
                       "direction": "ASC"
                    }
                }
        ],
        "offset": 0,
        "count": COUNT_PER_PAGE
    }

    result = client.execute(query)

    return result["games"]["nodes"]

# <https://graphql.nexusmods.com/#definition-ModFileContentSearchFilter>
def build_file_paths_filter(with_extensions: list[str], with_file_names: list[str]):
    if with_file_names:
        return {
            'op': 'OR',
            'fileNameWildcard': [{'op': 'EQUALS', 'value': x} for x in with_file_names]
        }

    return {
        "op": "OR",
        "fileExtensionExact": [{"op": "EQUALS", "value": x} for x in with_extensions]
    }

# <https://graphql.nexusmods.com/#query-modFileContents>
def get_files_page(client, game_id, file_paths_filter, starting_file_id, offset):
    logging.info(f'Getting page starting at file ID {starting_file_id} and offset {offset}...')

    query = gql("""
        query modFileContents(
            $filter: ModFileContentSearchFilter,
            $sort: [ModFileContentSearchSort!],
            $offset: Int,
            $count: Int
        ) {
            modFileContents(
                filter: $filter,
                sort: $sort,
                offset: $offset,
                count: $count
            ) {
                nodes {
                    fileName,
                    fileId,
                    gameId,
                    modId
                }
                nodesCount,
                totalCount
            }
        }
        """)

    query.variable_values = {
        "filter": {
            "op": "AND",
            "gameId": {
                "op": "EQUALS",
                "value": game_id
            },
            "fileId": {
                "op": "GTE",
                "value": starting_file_id
            },
            "filter": file_paths_filter,
        },
        "sort": {
            "fileId": {
                "direction": "ASC"
            }
        },
        "offset": offset,
        "count": COUNT_PER_PAGE
    }

    result = client.execute(query)

    return result['modFileContents']

def get_all_files(client, game_id, file_paths_filter):
    logging.info(f'Getting mod IDs and filenames for the game with ID {game_id}')

    entries = []
    starting_file_id = 0
    offset = 0
    page = get_files_page(client, game_id, file_paths_filter, starting_file_id, offset)
    entries += page["nodes"]

    logging.info(f'There are a total of {page["totalCount"]} entries to get')

    while len(page["nodes"]) == COUNT_PER_PAGE:
        last_file_id = page["nodes"][-1]["fileId"]
        if last_file_id == starting_file_id:
            offset += len(page["nodes"])
        else:
            starting_file_id = last_file_id
            offset = len([n for n in page["nodes"] if n["fileId"] == last_file_id])

        sleep(SECS_BETWEEN_REQS)

        page = get_files_page(client, game_id, file_paths_filter, starting_file_id, offset)
        entries += page["nodes"]

    return entries

def find_game_id(games, game_name):
    for game in games:
        if game["name"] == game_name:
            return game["id"]

    return None

def get_url(games, game_id, mod_id):
    for game in games:
        if game["id"] == game_id:
            return f'https://www.nexusmods.com/{game["domainName"]}/mods/{mod_id}'

    return None

def write_to_tsv(output_path, games, files):
    with open(output_path, 'w', newline='', encoding='utf8') as tsv_file:
        field_names = ['fileName', 'gameId', 'fileId', 'modId', 'url']
        writer = csv.DictWriter(tsv_file, delimiter='\t', fieldnames=field_names)

        writer.writeheader()
        for file in files:
            writer.writerow({
                "fileName": file["fileName"],
                "gameId": file["gameId"],
                "fileId": file["fileId"],
                "modId": file["modId"],
                "url": get_url(games, file["gameId"], file["modId"])
            })

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('-m', '--masterlist-path')
    parser.add_argument('-o', '--output-path')
    parser.add_argument('-g', '--game', choices=GAME_NAMES, required=True)
    parser.add_argument('-l', '--log-path', default=Path(__file__).with_suffix('.log'))
    parser.add_argument('-s', '--log-severity', default='info', choices=['debug', 'info', 'warning', 'error'])
    args = parser.parse_args()

    logging.basicConfig(filename=args.log_path, filemode='w', level=args.log_severity.upper())
    logging.getLogger().addHandler(logging.StreamHandler())

    game_name = args.game
    output_path = args.output_path if args.output_path else Path.cwd() / 'data' / f"nexus_mods_{game_name.lower().replace(' ', '_')}_plugins.tsv"

    filter_for_plugin_names = None
    if args.masterlist_path:
        filter_for_plugin_names = find_plugins_without_urls(args.masterlist_path)

        logging.info(f'Filtering for {len(filter_for_plugin_names)} plugin names')

    transport = AIOHTTPTransport(url=API_URL)

    client = Client(transport=transport)

    games = get_games(client)

    game_id = find_game_id(games, game_name)

    logging.info(f'Game ID for {game_name} is {game_id}')

    file_paths_filter = build_file_paths_filter(PLUGIN_FILE_EXTENSIONS, filter_for_plugin_names)
    files = get_all_files(client, game_id, file_paths_filter)

    files.sort(key=lambda f: (f['fileName'], f['modId'], f['fileId']))

    write_to_tsv(output_path, games, files)
