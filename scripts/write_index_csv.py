#!/usr/bin/env python

# This script writes an index TSV file that can be used as input into the
# read_archive_plugins.py script. However, once written the TSV file must be
# manually updated to add mod names, hosting site names and download URLs to the
# Name, Site and Link columns respectively.

import argparse
import csv
import hashlib
import logging
import os
from pathlib import Path

def write_to_tsv(output_path, archives):
    with open(output_path, 'w', newline='', encoding='utf8') as tsv_file:
        field_names = ['Id', 'SHA-256', 'MD5', 'Name', 'Site', 'Link']
        writer = csv.DictWriter(tsv_file, delimiter='\t', fieldnames=field_names)

        writer.writeheader()
        for archive in archives:
            writer.writerow(archive)

def calculate_sha256(file_path):
    with open(file_path, 'rb') as input:
        return hashlib.sha256(input.read()).hexdigest()

def calculate_md5(file_path):
    with open(file_path, 'rb') as input:
        return hashlib.md5(input.read()).hexdigest()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('-d', '--downloads-path', default=Path.home() / "Downloads" / 'manual_morrowind_mod_downloads')
    parser.add_argument('-o', '--output-path', default=Path.cwd() / 'data' / 'manual_downloads_index.tsv')
    parser.add_argument('-l', '--log-path', default=Path(__file__).with_suffix('.log'))
    parser.add_argument('-s', '--log-severity', default='info', choices=['debug', 'info', 'warning', 'error'])
    args = parser.parse_args()

    logging.basicConfig(filename=args.log_path, filemode='w', level=args.log_severity.upper())
    logging.getLogger().addHandler(logging.StreamHandler())

    archives = []
    for entry in os.scandir(args.downloads_path):

        # The hashes are included to help with manually double-checking that you've downloaded the
        # files that you think you have: ModDB displays the MD5 hash of its downloads.
        archives.append({
            'Id': entry.name,
            'SHA-256': calculate_sha256(entry.path),
            'MD5': calculate_md5(entry.path),
            'Name': '',
            'Site': '',
            'Link': ''
        })

    write_to_tsv(args.output_path, archives)
