Contributing To The Masterlist
==============================

## Filing Issues

If you're about to file an issue for some suggested change, please consider instead making the change yourself and submitting it as a pull request. Pull requests are much more likely to be quickly acted on, so they're the best way `to get your change into the masterlist.

## Making Changes

LOOT's masterlists are [versioned](https://loot.github.io/docs/contributing/Masterlist-Versioning). Before making any changes, please make sure that you're working on the correct branch for LOOT's latest release.

If you are new to contributing to projects on GitHub, the [How To Contribute](https://loot.github.io/docs/contributing/How-To-Contribute) wiki page is a good starting point.

The format and syntax of the masterlist is [fully documented](https://loot-api.readthedocs.io/en/stable/metadata/introduction.html), and other information on how to edit the masterlist can be found on the [Masterlist Editing](https://loot.github.io/docs/contributing/Masterlist-Editing) wiki page.

When submitting changes, please keep them as focused as possible. If you'd like to make several unrelated changes across multiple mods, open separate pull requests for each mod you edit. This makes reviewing your changes much easier, keeps the commit history organized and easier to navigate, and makes reverting changes easier when necessary. Note that if a mod contains multiple plugins, you can make changes to any of those plugins in the same pull request. If you want to make a change to a separate mod that is related to the mod you're editing (e.g., you're editing Mod A's metadata & Mod B should load after it), you can include that in the same pull request as well. You can also edit several unrelated mods if your changes are contained to a single topic, e.g., updating Bash Tags or cleaning data. If, for example, you're updating the Bash Tags of several mods and would like to add cleaning data for them as well, you should do that in a separate pull request.

If you send a pull request, then discover that you have made a mistake, feel free to make additional commits to fix it. You don't need to squash your commits or open a new pull request to keep your commit history tidy; whoever merges your pull request can squash it for you. You can also mark your pull request as a draft if it isn't ready to be merged.

### Testing Changes

Pull requests and all commits to the masterlist repository are automatically run through a validator to check for syntax errors.

Nevertheless, it is advisable to test your changes on your own LOOT install before sending a pull request, as they may have valid syntax but not have the intended effect. Instructions on how to do so can be found on [LOOT's wiki](https://loot.github.io/docs/contributing/Quickly-Testing-Your-Masterlist-Changes).

## Migrating mlox rules

The `*.txt` files in the `mlox` directory are copies from other repositories:

| File | Source |
|------|--------|
| `mlox_base.txt` | https://raw.githubusercontent.com/mlox/mlox/bd5b39e4704d4574e2a7be3d0a598a686a646214/data/mlox_base.txt |
| `mlox_base_legacy.txt` | https://raw.githubusercontent.com/DanaePlays/mlox-rules/91352cb0611cf5381e72c4cb5b6a3aec5a72d258/mlox_base_legacy.txt |
| `mlox_base.danae.txt` | https://raw.githubusercontent.com/DanaePlays/mlox-rules/91352cb0611cf5381e72c4cb5b6a3aec5a72d258/mlox_base.txt |
| `mlox_user.txt` | https://raw.githubusercontent.com/DanaePlays/mlox-rules/91352cb0611cf5381e72c4cb5b6a3aec5a72d258/mlox_user.txt |

The corresponding `*.yaml` files are LOOT metadata files that were generated
from those mlox rules files using the script at `scripts/import_mlox.py`, and
the files in the `data` directory, which were generated from the other scripts
in the `scripts` directory.

While the import script can handle converting the metadata in most cases, not
all functionality supported by LOOT is supported by mlox. The script logs the
details of any issues, limitations or errors that it encounters, and the `*.log`
file for each run is included in the `mlox` directory beside the relevant input
and output files.

### Filename patterns

The most common issue is that mlox supports the use of filename patterns in more
places than LOOT supports the use of regular expressions. The import script
tries to deal with this by matching the patterns to real filenames that appear
in the `data` files it was given, but it doesn't always find matches. Its
handling also varies by context:

- In mlox SIZE predicates:
    - If matches were found, the predicate is replaced with a condition for each
      match, all joined by 'or' operators. For example,
      `file_size("example.*\.esp")` could become
      `(file_size("example1.esp") or file_size("example2.esp"))`.
    - If no matches were found, the predicate is turned into a single
      `file_size` condition that uses the pattern as a filename. This condition
      will never evaluate to true, since the pattern cannot be a valid Windows
      filename.
    - There is only one instance of a SIZE predicate that uses a pattern, and
      it's `[SIZE !4495981 Balmora Expansion v1.4+(<VER>).esp]`. That pattern
      matches only one known filename from one hosted archive, and that file's
      size is `4495981`, so the message that the predicate is attached to seems
      redundant.
- In mlox DESC predicates:
    - The behaviour is the same as for SIZE predicates, except that the
      equivalent LOOT condition is `description_contains()`, not `file_size()`.
    - There are no cases of this occurring in the imported mlox rules.
- In mlox ORDER rules:
    - If matches were found, they are listed as `after` file entries after the
      original pattern. The pattern is not a valid filename, so will never match
      anything, which is harmless in this context. It is retained as a signal
      that pattern matching was used: you can remove it if you're happy that the
      outcome of that matching makes sense.
    - If matches were not found, the original pattern is included as an `after`
      file entry. The entry cannot be a valid Windows filename, so should never
      match a real file's name.
- In mlox REQUIRES rules:
    - If matches were found, they replace the pattern as `req` file entries.
    - If matches were not found, no `req` file entries are written for the
      pattern. This is treated as a rule conversion error, and did not occur for
      any of the rules committed in this repository.
- In mlox CONFLICT rules:
    - If matches were found, they replace the pattern as `inc` file entries.
    - If matches were not found, the original pattern is included as an `inc`
      file entry. The entry cannot be a valid Windows filename, so should never
      match a real file's name.

The log messages for when matches were found all contain the string
`the following matching filenames:`, followed by the matching filenames. The log
messages for when no matches were found all contain the string
`No matching filenames were found`. You can use those strings to filter the logs
for the log messages that you're interested in.

One issue with this pattern matching approach is that there may be matching
filenames available for download that aren't in the set of filenames known to
the import script. It's unlikely, given that the `data` files list almost 40000
plugin filenames (with many duplicates), but there are several smaller community
hosting websites that predate and/or have outlasted the larger and more popular
hosting websites.

It might help to leave a YAML comment giving the original pattern whether or not
any matches were found, and you could try searching online to see if you can
find any (other) matches, but it's also reasonable to just treat filling any
gaps as they're reported, the same as any other case where the masterlist needs
to be updated. Where mlox patterns are included in the output YAML, it's OK to
remove them once you're satisfied that their presence doesn't add value to
masterlist maintainers.

You can read the log files to see what other issues were encountered, but there
probably wasn't anything else that needs to be resolved manually: the next most
common warning is for VER predicates that use a pattern, but in practice there
is no difference in behaviour for any of the affected plugins that could be
located.

### Content style

Even mlox rules that are converted flawlessly probably can't be copy/pasted
as-is into the masterlist. This is usually due to mlox messages having a
different style compared to LOOT. Things to look out for are:

- Missing location metadata: while most plugin entries have location metadata,
  some don't. It's probably best to skip those entries, or you could try
  searching for them online in case they're still available on a smaller hosting
  site.

- Invalid URLs: the URLs in location metadata are all valid, but many messages
  include links to web pages that are no longer available. Some of those may be
  accessible on the Wayback Machine. There is a `check_urls.py` script, but its
  functionality is currently very limited: it might be extended to help automate
  checking and fixing URLs.

- Numbered anchors: while the import script can deduplicate some values using
  YAML anchors and aliases, it can't give the anchors meaningful names, so it's
  best to replace them with more descriptive names.

- Very similar content strings that could be replaced with a single anchor and
  the use of aliases, merge keys and `subs` values.

- Redundant detail content strings: mlox requires and conflict rules share a
  single message across multiple plugins, and that message is often very similar
  to what the messages that LOOT generates for requirement and incompatibility
  metadata say.

- Multi-line messages: mlox and LOOT both support multi-line strings, but to
  simplify how the import script writes them in YAML, they're written as
  double-quoted strings with their line breaks escaped. Unfortunately, that
  makes them relatively difficult to read.

  Here's an example, these `text` values are the same, but one is much easier to
  read:

  ```yaml
  contents:
    - text: "This is a\nmulti-line\ncontent string"
    - text: |
        This is a
        multi-line
        content string
  ```

  However, before reformatting a multi-line message, check if it really benefits
  from having multiple lines: in the LOOT masterlists there are approximately
  zero multi-line content strings. It might be possible to rewrite the message
  to be clearer as a single sentence or paragraph.

- Unnecessary escapes in CommonMark content strings: mlox messages are written
  in plain text, while LOOT's are in CommonMark, so the import script escapes
  any characters that may have special meaning in CommonMark. It tries to omit
  unnecessary escape sequences, but it's conservative in doing so, and there
  will be cases where a human can easily spot that some escapes can be removed.

- `(Ref: ...)` in content strings: mlox messages like to reference where they
  got their information from. Ask if having that in the message really adds
  value to the reader. It may be more appropriate to leave it in a YAML comment,
  or in a Git commit message, if it's worth keeping at all.

- Sometimes mlox messages are worded so that they only make sense if you can
  see all of plugins that are listed in the rule, for example
  `Use only one of these plugins.`. That particular message is a good example of
  a redundant file detail string and could be removed entirely, but others may
  need to be rewritten to make sense when the reader can only see the plugin
  it appears under in LOOT's user interface.

- Punctuation used as plain text formatting, or included unnecessarily: some
  messages are wrapped in extra double or single quotes, some use square
  brackets as if they were quotes or monospacing delimiters, or sometimes for
  text that looks like it could be redacted. These should be converted to the
  equivalent CommonMark syntax where relevant, and otherwise stripped.

- mlox messages don't support any markup for URLs to turn them into hyperlinks,
  so they should be converted to avoid bare URLs where they can be given useful
  text. For example, the YAML:

  ```yaml
  detail: "Download the patch for Taddeus' \"Necessities of Morrowind\" and cml33's \"Census and Excise Office Quarters\" from Morrowind Nexus -\nhttp://www.nexusmods.com/morrowind/mods/43205/\n(Ref: \"Census and Excise Office Quarters NOM Patch.txt\")"
  ```

  could be rewritten as:

  ```yaml
  detail: 'Download the patch for Taddeus''s "Necessities of Morrowind" and cml33''s "Census and Excise Office Quarters" from [Nexus Mods](https://www.nexusmods.com/morrowind/mods/43205/).'
  ```

If something in a message doesn't make sense, you can try looking up the source
mlox rule. Comments in the mlox rules are not carried over to the YAML metadata,
and may provide useful information.

### Replacing invalid URLs

Some of the URLs in the generated masterlists are invalid: in some cases, that
just means that they don't point to relevant content any more, but in other
cases the domains that the URLs point to have been repurposed to spread malware.

The `data/urls.tsv` file contains a table of all the URLs in the generated
masterlists, including their status and potential replacement URLs. See the
comments in `scripts/check_urls.py` for explanations of the different statuses
and what to do with them.

In short, URLs with `VALID_VERIFIED` or `VALID_FROM_INDEX` status don't need to
be replaced, but they may still have suggested replacements (e.g. making them
use HTTPS, or suggesting the URL that the existing URL now redirects to). For
all other statuses, the URL needs to be removed, replaced, or more investigation
is needed.

### Cyclic order rules

mlox allows its metadata to produce cycles, and it'll just ignore whatever the
last-read rule was that caused the cycle. LOOT requires its `after` and `req`
metadata to not cause cycles.

It's probably not feasible to do anything about this proactively, but some
metadata may need to be made conditional if it turns out that it causes cycles
otherwise.

### Regenerating the YAML files

To regenerate the YAML files, run:

```
uv run `
    --with libloot@../libloot/target/wheels/libloot-0.28.4-cp314-cp314-win_amd64.whl `
    --python python `
    -- `
    scripts/import_mlox.py `
    -p data/mmh_fliggerty_plugins.tsv `
    -p data/nexus_mods_morrowind_plugins.tsv `
    -p data/manual_mod_plugins.tsv `
    -i mlox/<txt file>
```

from a PowerShell terminal (to use a POSIX-like shell, e.g. `bash`, replace the
trailing backticks with backslashes), replacing `<txt file>` with the relevant
input filename.

The command above expects the libloot Python wrapper to have been built from
source in a sibling directory, as prebuilt binaries are not currently published.
See [its README](https://github.com/loot/libloot/blob/master/python/README.md)
for how to build it: you need Rust and Python to be installed first.

### Regenerating the data files

`manual_downloads_index.tsv` was partially generated from a set of Morrowind
Rebirth downloads from ModDB using `scripts/write_index_tsv.py`, and should be
appended to rather than replaced.

`manual_mod_plugins.tsv` was generated from the same set of downloads and
`manual_downloads_index.tsv` using `scripts/read_archive_plugins.py`. The same
script can be run to regenerate the file.

`MMH_&_Fliggerty_Mods.csv` was downloaded from
<https://modlist.altervista.org/mmh/> using the Export -> "Export to Csv" option
on that page. As the CSV represents a historical snapshot of mods, it probably
won't need to be regenerated.

`mmh_fliggerty_plugins.tsv` was generated by first running
`scripts/download_mmh_fliggerty_mods.py` to download the mods listed in
`MMH_&_Fliggerty_Mods.csv`, and then running `scripts/read_archive_plugins.py`
against that same CSV file and the downloaded files. The total size of the
downloads is over 61 GB, and again it's a historical snapshot, so this probably
won't need to be regenerated.

`nexus_mods_morrowind_plugins.tsv` was generated by running
`scripts/fetch_nexus_plugins_index.py`. It is a snapshot of the Morrowind
plugins hosted on Nexus Mods as of October 2025, so it might be worth
regenerating in the future.

`urls.tsv` was generated by running `scripts/check_urls.py` using the generated
masterlists and other TSV files as inputs, and then manually updated. It
shouldn't be regenerated from scratch, as that'll lose the manual updates, but
it can be provided as an input into the script to preserve those.
