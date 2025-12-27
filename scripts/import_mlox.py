#!/usr/bin/env python
# /// script
# requires-python = ">=3.8"
# dependencies = ["pyyaml==6.0.2"]
# ///

# This script parses the mlox rules into an abstract representation, then
# converts that to LOOT metadata objects, which are then serialised as YAML.
#
# The YAML serialisation is done either using libloot or using PyYAML: the
# latter is easier to install (it's on PyPI) but produces YAML of a very
# different style to LOOT's masterlists. libloot must be built from source (see
# <https://github.com/loot/libloot/blob/master/python/README.md>) and then made
# available to this script, e.g. using
# `uv run --with libloot@../libloot/target/wheels/libloot-0.28.4-cp314-cp314-win_amd64.whl --python python -- scripts/import_mlox.py ...`.
#
# This script doesn't attempt to match mlox's parser behaviour exactly (e.g.
# mlox treats whitespace as optional in more places), but instead targets the
# syntax that is actually in use in the https://github.com/mlox/mlox and
# https://github.com/DanaePlays/mlox-rules repositories.
#
# The conversion logic cannot handle all possible combinations of mlox
# expressions, but it is able to fully convert the mlox rules files in the above
# repositories, and logs warnings for any rules that it thinks it cannot fully
# convert. The conversion will error if it encounters a rule that seems invalid.
#
# The script will also log info messages for intentional changes to the mlox
# rules, including:
#
# - LOOT does not support <hide></hide> spoiler tags, and will strip them and
#   their contents from messages.
# - LOOT does not support blue-highlighted messages, so they will be treated as
#   normal unhighlighted messages.
# - Requires rules are converted to incompatibility metadata when it looks like
#   the requirement is that a plugin should not be installed.
# - Conflict rules with a message and only one expression are converted to
#   message metadata.
# - Conflict rules may be converted to requirement metadata when it looks like
#   the conflict is reported for a file that is not installed.
# - Some conflict rules with nested [NOT [ANY ... ]] expressions may have them
#   transformed into [ALL [NOT ...] ...] rules during conversion.
#
# Warnings are also logged when syntax or content is encountered that can be
# converted but that may not be understood by LOOT. These include:
#
# - mlox's VER predicate first tries to read a plugin's version from its
#   description and then falls back to reading it from its filename if no
#   version is found in the description, then performs the comparison using the
#   version it found. LOOT checks description and filename versions separately,
#   and if this script sees that the filename in the VER predicate is a pattern
#   (i.e. contains ? * or <VER>) it will only compare the filename version.
# - mlox's filename version matching looks for any substring of a filename that
#   matches a version regex, and uses the first match as the version value, but
#   LOOT expects the filename regex to contain a single capturing group for the
#   version substring. This script converts <VER> constructs to such a capturing
#   group, but does not do the same for ? or * characters, because there are
#   cases where they appear multiple times within the same pattern.
# - LOOT's equivalents to the SIZE and DESC predicates only support exact
#   filenames, so this script attempts to match filename patterns in those
#   predicates against known filenames, and uses any matches instead.
# - mlox's Order rules are converted to "load after" metadata, but the former
#   supports filename patterns but the latter does not, so this script attempts
#   to match filename patterns against known filenames, and inserts any matches.
# - mlox's Conflict rules are converted to incompatibility metadata, but the
#   former supports filename patterns but the latter does not, so this script
#   attempts to match filename patterns against known filenames, and inserts any
#   matches.
# - mlox's Requires and Patch rules are converted to requirement metadata, but
#   the mlox rules support filename patterns and the latter does not, so this
#   script attempts to match filename patterns against known filenames, and
#   replaces the pattern with any matches. If there are no matches, the entry is
#   skipped.
#
# The known filenames that the script attempts to match patterns against are the
# non-pattern filenames that appear in the input mlox rules.
#
# It's possible to provide this script with TSV (CSV, but tab-separated) files
# that have modId, fileName and url column headings. Any filenames in the TSV
# files will be added to the set of known filenames, and the URLs will be used
# to add URL metadata to any LOOT metadata entries for those filenames. If the
# TSV data has a modName column, it will be used to set the name field of the
# relevant location metadata.

import argparse
import csv
from enum import auto, Enum
from pathlib import Path
from typing import Any, NamedTuple
import logging
import math
import re

try:
    import loot
    loot_found = True
except ImportError:
    print(f'Failed to import the loot module, the yaml module will be used to write YAML instead')
    loot_found = False
    import yaml

VER_REGEX_STR = r'(\d+(?:[_.-]?\d+)*[a-z]?)'
HIDE_TAGS_REGEX = re.compile(" <hide>[^<]+</hide>")
GROUP_NEAR_START = 'Near Start'
GROUP_NEAR_END = 'Near End'

# These are global caches used to deduplicate log entries.
SIZE_PATTERNS = set()
DESC_PATTERNS = set()
AFTER_PATTERNS = set()
REQ_PATTERNS = set()
INC_PATTERNS = set()
BLUE_MESSAGE_CONTENT = set()

# These are global caches used to deduplicate objects in memory, so that shared
# objects will be anchored/aliased when emitted as YAML.
# The keys are hashes and the values are the objects that produce those hashes.
INTERNED_MESSAGES = {}
INTERNED_FILES = {}
INTERNED_STRINGS = {}

class RuleType(Enum):
    NEAR_START = auto()
    NEAR_END = auto()
    ORDER = auto()
    NOTE = auto()
    REQUIRES = auto()
    CONFLICT = auto()
    PATCH = auto()

class Operator(Enum):
    EQUALS = auto()
    LESS_THAN = auto()
    GREATER_THAN = auto()

class Highlight(Enum):
    NONE = auto()
    BLUE = auto()
    YELLOW = auto()
    RED = auto()

class Message(NamedTuple):
    highlight: Highlight
    text: str

type Expression = FileExpression | VersionExpression | SizeExpression | DescriptionExpression | NotExpression | AnyExpression | AllExpression

class FileExpression(NamedTuple):
    file: str

class VersionExpression(NamedTuple):
    operator: Operator
    version: str
    file: str

class SizeExpression(NamedTuple):
    negated: bool
    size: int
    file: str

class DescriptionExpression(NamedTuple):
    negated: bool
    regex: str
    file: str

class NotExpression(NamedTuple):
    expressions: list[Expression]

class AnyExpression(NamedTuple):
    expressions: list[Expression]

class AllExpression(NamedTuple):
    expressions: list[Expression]

class NearStartRule(NamedTuple):
    plugins: list[str]

class NearEndRule(NamedTuple):
    plugins: list[str]

class OrderRule(NamedTuple):
    plugins: list[str]

class NoteRule(NamedTuple):
    message: Message | None
    expressions: list[Expression]

class RequiresRule(NamedTuple):
    message: Message | None
    dependent: Expression
    consequent: Expression

class ConflictRule(NamedTuple):
    message: Message | None
    expressions: list[Expression]

class PatchRule(NamedTuple):
    message: Message | None
    patch: Expression
    original: Expression

type Rule = NearStartRule | NearEndRule | OrderRule | NoteRule | RequiresRule | ConflictRule | PatchRule

class ModPlugin(NamedTuple):
    mod_id: str
    mod_name: str | None
    plugin_name: str
    url: str

def find_end_of_plugin_filename(string: str, start_pos: int):
    window_size = 5
    for i in range(start_pos, len(string) - window_size + 1):
        window = string[i:i+window_size].lower()
        if window == '.esp ' or window == '.esm ':
            return i + window_size - 1

    return None

def append_expressions(expressions: list[str], expressions_slice: str):
    if not expressions_slice:
        return

    if expressions_slice.startswith('['):
        expressions.append(expressions_slice)
        return

    # Could be one or more plugin filenames.
    pos = 0
    while pos < len(expressions_slice):
        end_of_filename = find_end_of_plugin_filename(expressions_slice, pos)
        if end_of_filename:
            expression = expressions_slice[pos:end_of_filename].strip()
            if expression:
                expressions.append(expression)
            pos = end_of_filename + 1
        else:
            expression = expressions_slice[pos:].strip()
            if expression:
                expressions.append(expression)
            break

def is_structured_expression_start(s: str):
    s = s[:5].upper()
    return s.startswith('[ANY') or s.startswith('[ALL') or s.startswith('[NOT') or s.startswith('[DESC') or s.startswith('[SIZE') or s.startswith('[VER')

def read_expressions(lines: list[str]) -> list[str]:
    expressions_str = ' '.join(lines)

    expressions: list[str] = []
    expression_start_index = 0
    open_braces = 0
    in_structured_expression = False
    for index, c in enumerate(expressions_str):
        if c == '[':
            # [ can appear in filenames, so check if this looks like the start of an expression.
            if is_structured_expression_start(expressions_str[index:]):
                in_structured_expression = True

            if open_braces == 0 and in_structured_expression:
                append_expressions(expressions, expressions_str[expression_start_index:index])
                expression_start_index = index

            open_braces += 1
        elif c == ']':
            open_braces -= 1

            if open_braces == 0 and in_structured_expression:
                append_expressions(expressions, expressions_str[expression_start_index:index+1])
                expression_start_index = index + 1
                in_structured_expression = False

    append_expressions(expressions, expressions_str[expression_start_index:])

    if open_braces != 0:
        logging.error(f'Found mismatched square brackets when reading expressions in: {lines}')
        raise RuntimeError(f'Found mismatched square brackets when reading expressions')

    return expressions

def find_whitespace(s: str, start: int):
    for index, c in enumerate(s[start:]):
        if c.isspace():
            return start + index

    return -1

def parse_description_expression(expression: str):
    negated = False
    regex_start = None
    for index, c in enumerate(expression[5:]):
        if c == '!':
            negated = True
        elif c == '/':
            regex_start = index + 6
            break

    if not regex_start:
        logging.error(f'Could not find regex in description expression {expression}')
        raise RuntimeError('Could not find regex in description expression')

    file_start = expression.rfind('/') + 1

    regex = expression[regex_start:file_start-1]
    file = expression[file_start:-1].strip()

    return DescriptionExpression(negated, regex, file)

def parse_size_expression(expression: str):
    negated = False
    size_start = None
    for index, c in enumerate(expression[5:]):
        if c == '!':
            negated = True
        elif c.isdigit():
            size_start = index + 5
            break

    if not size_start:
        logging.error(f'Could not find size in size expression {expression}')
        raise RuntimeError('Could not find size in size expression')

    assert(isinstance(size_start, int))
    file_start = find_whitespace(expression, size_start)

    size = int(expression[size_start:file_start])
    file = expression[file_start:-1].strip()

    return SizeExpression(negated, size, file)

def parse_version_expression(expression: str):
    operator = None
    version_start = None
    for index, c in enumerate(expression[4:]):
        if c == '<':
            operator = Operator.LESS_THAN
        elif c == '=':
            operator = Operator.EQUALS
        elif c == '>':
            operator = Operator.GREATER_THAN
        elif not c.isspace():
            version_start = index + 4
            break

    if not operator:
        logging.error(f'Could not find operator in version expression {expression}')
        raise RuntimeError('Could not find operator in version expression')

    assert(isinstance(version_start, int))
    file_start = find_whitespace(expression, version_start)

    version = expression[version_start:file_start].strip()
    file = expression[file_start:-1].strip()

    return VersionExpression(operator, version, file)

def parse_expression(expression: str):
    if expression[:4].upper().startswith('[ANY'):
        inner_expressions = read_expressions([expression[4:-1]])

        return AnyExpression([parse_expression(e) for e in inner_expressions])
    elif expression[:4].upper().startswith('[ALL'):
        inner_expressions = read_expressions([expression[4:-1]])

        return AllExpression([parse_expression(e) for e in inner_expressions])
    elif expression[:4].upper().startswith('[NOT'):
        inner_expressions = read_expressions([expression[4:-1]])

        return NotExpression([parse_expression(e) for e in inner_expressions])
    elif expression[:5].upper().startswith('[DESC'):
        return parse_description_expression(expression)
    elif expression[:5].upper().startswith('[SIZE'):
        return parse_size_expression(expression)
    elif expression[:4].upper().startswith('[VER'):
        return parse_version_expression(expression)
    else:
        # A bare filename, representing a plugin that needs to be active.
        if '[' in expression or ']' in expression:
            logging.debug(f'Could be mis-parsed expression: {expression}')

        return FileExpression(expression.strip())

def parse_message_and_expressions(rule_lines: list[str]):
    highlight = Highlight.NONE
    text = ''

    first_expression_index = len(rule_lines)
    for index, line in enumerate(rule_lines):
        if line[0].isspace():
            # Message text line
            line = line.strip()
            if line.startswith('!!! '):
                highlight = Highlight.RED
                text += line[4:] + '\n'
            elif line.startswith('!! '):
                highlight = Highlight.YELLOW
                text += line[3:] + '\n'
            elif line.startswith('! '):
                highlight = Highlight.BLUE
                text += line[2:] + '\n'
            else:
                text += line + '\n'
        else:
            first_expression_index = index
            break

    expressions_lines = rule_lines[first_expression_index:]

    expressions = read_expressions(expressions_lines)
    expressions = [parse_expression(e) for e in expressions]

    if text:
        return (Message(highlight, text.strip()), expressions)
    else:
        return (None, expressions)

def to_note_rule(rule_lines: list[str]):
    message, expressions = parse_message_and_expressions(rule_lines)

    return NoteRule(message, expressions)

def to_requires_rule(rule_lines: list[str]):
    message, expressions = parse_message_and_expressions(rule_lines)

    if len(expressions) != 2:
        logging.error(f'Rule has the wrong number of expressions: {rule_lines} -> {expressions}')
        raise RuntimeError('Encountered a Requires with the wrong number of expressions')

    if message:
        message = Message(Highlight.RED, message.text)

    return RequiresRule(message, expressions[0], expressions[1])

def to_conflict_rule(rule_lines: list[str]):
    message, expressions = parse_message_and_expressions(rule_lines)

    if message:
        message = Message(Highlight.YELLOW, message.text)

    return ConflictRule(message, expressions)

def to_patch_rule(rule_lines: list[str]):
    message, expressions = parse_message_and_expressions(rule_lines)

    if len(expressions) != 2:
        logging.error(f'Rule has the wrong number of expressions: {rule_lines} -> {expressions}')
        raise RuntimeError('Encountered a Patch with the wrong number of expressions')

    if message:
        message = Message(Highlight.YELLOW, message.text)

    return PatchRule(message, expressions[0], expressions[1])

def to_rule(rule_type: RuleType, rule_lines: list[str]) -> Rule:
    match rule_type:
        case RuleType.NEAR_START:
            return NearStartRule(rule_lines)
        case RuleType.NEAR_END:
            return NearEndRule(rule_lines)
        case RuleType.ORDER:
            return OrderRule(rule_lines)
        case RuleType.NOTE:
            return to_note_rule(rule_lines)
        case RuleType.REQUIRES:
            return to_requires_rule(rule_lines)
        case RuleType.CONFLICT:
            return to_conflict_rule(rule_lines)
        case RuleType.PATCH:
            return to_patch_rule(rule_lines)
        case _:
            logging.error(f'Unrecognised rule type: {rule_type}')
            raise RuntimeError('Unrecognised rule type')

def strip_comment(line: str):
    comment_pos = line.find(';')
    if comment_pos != -1:
        line = line[:comment_pos]

    return line.rstrip()

def get_rule_type(line):
    # This doesn't support the inline rule syntax, but that doesn't seem to be used in practice.

    if not line.startswith('[') or not line.endswith(']'):
        return None

    rule_type  = line[1:-1].lower()
    match rule_type:
        case 'nearstart':
            return RuleType.NEAR_START
        case 'nearend':
            return RuleType.NEAR_END
        case 'order':
            return RuleType.ORDER
        case 'note':
            return RuleType.NOTE
        case 'requires':
            return RuleType.REQUIRES
        case 'conflict':
            return RuleType.CONFLICT
        case 'patch':
            return RuleType.PATCH
        case _:
            return None

def read_rules(stream) -> list[Rule]:
    rules = []
    rule_type = None
    rule_lines: list[str] = []

    for line in stream.readlines():
        if line.startswith(';'):
            continue

        if line.startswith('[Version'):
            # Do nothing, this seems to be file metadata.
            continue

        line = strip_comment(line)

        if not line:
            continue

        new_rule_type = get_rule_type(line)
        if new_rule_type:
            if rule_type:
                rules.append(to_rule(rule_type, rule_lines))

            rule_type = new_rule_type
            rule_lines = []
        else:
            rule_lines.append(line)

    assert(isinstance(rule_type, RuleType))
    rules.append(to_rule(rule_type, rule_lines))

    return rules

################################################################################
# End of parsing code, start of plugin name matching code
################################################################################

def add_if_missing(value: str, value_set: set[str]):
    folded = value.casefold()
    if folded not in value_set:
        value_set.add(folded)
        return True

    return False

def deduplicate(values: list[str]) -> list[str]:
    unique = []
    unique_case_folded = set()
    for value in values:
        if add_if_missing(value, unique_case_folded):
            unique.append(value)

    return unique

def get_filenames_from_expression(expression: Expression) -> set[str]:
    match expression:
        case FileExpression(file) | VersionExpression(_, _, file) | SizeExpression(_, _, file) | DescriptionExpression(_, _, file):
            return set([file]) if is_valid_filename(file) else set()
        case NotExpression(expressions) | AnyExpression(expressions) | AllExpression(expressions):
            filenames: set[str] = set()
            for e in expressions:
                filenames.update(get_filenames_from_expression(e))
            return filenames

def get_filenames(rules: list[Rule]) -> set[str]:
    filenames: set[str] = set()
    for rule in rules:
        match rule:
            case NearStartRule(plugins) | NearEndRule(plugins) | OrderRule(plugins):
                filenames.update(p for p in plugins if is_valid_filename(p))
            case NoteRule(_, expressions) | ConflictRule(_, expressions):
                for e in expressions:
                    filenames.update(get_filenames_from_expression(e))
            case RequiresRule(_, expression1, expression2) | PatchRule(_, expression1, expression2):
                filenames.update(get_filenames_from_expression(expression1))
                filenames.update(get_filenames_from_expression(expression2))
            case _:
                logging.error(f'Unrecognised rule type: {type(rule)}')
                raise RuntimeError(f'Unrecognised rule type: {type(rule)}')

    return filenames

def find_matching_plugins(filename: str, plugins_index: list[ModPlugin], plugins_index_map: dict[str, list[ModPlugin]]):
    logging.debug(f'Looking for matches for the filename {filename} in the plugins index')

    if is_valid_filename(filename):
        folded_filename = filename.casefold()
        if folded_filename in plugins_index_map:
            return plugins_index_map[folded_filename]
        else:
            return []

    # This filename doesn't need to be expanded because it comes from an
    # already-converted LOOT plugin metadata entry.
    pattern = re.compile(filename, flags=re.IGNORECASE)

    return [p for p in plugins_index if pattern.fullmatch(p.plugin_name)]

def find_matching_plugin_names(filename: str, known_filenames: set[str]):
    logging.debug(f'Looking for matches for the filename {filename} in the set of known filenames')

    if is_valid_filename(filename):
        matches = [f for f in known_filenames if filename.casefold() == f.casefold()]
    else:
        pattern = re.compile(expand_filename(filename), flags=re.IGNORECASE)

        matches = sorted(f for f in known_filenames if pattern.fullmatch(f))

    return deduplicate(matches)


################################################################################
# End of plugins index code, start of YAML emitter code
################################################################################

def write_masterlist(masterlist, output_path: str):
    if loot_found:
        # This writes YAML that's a closer match to the style of
        # manually-written masterlist entries, except that multi-line messages
        # have their line breaks escaped.
        game = loot.Game(loot.GameType.Morrowind, '.')
        db = game.database()

        set_db_groups(db, masterlist)

        set_db_general_messages(db, masterlist)

        set_db_plugins(db, masterlist)

        options = loot.MetadataWriteOptions()
        options.truncate = True
        options.write_anchors = True
        options.write_common_section = True
        options.anchor_file_strings = False

        db.write_user_metadata(output_path, options)

        # Sanity check that the metadata is valid.
        db.load_userlist(output_path)
    else:
        # This is a bit faster but the style is very different to
        # manually-written masterlist entries.
        with open(output_path, mode='w', encoding='utf8') as output:
            yaml.add_representer(InternedString, interned_string_representer)
            yaml.dump(masterlist, output, allow_unicode=True, width=math.inf, sort_keys=False)

def set_db_groups(db, masterlist):
    db_groups = [to_db_group(g) for g in masterlist['groups']]
    db.set_user_groups(db_groups)

def to_db_group(group):
    description = some_or_none(group, 'description')
    after = some_or_none(group, 'after')

    return loot.Group(group['name'], description, after)

def set_db_general_messages(db, masterlist):
    if masterlist['globals']:
        db_messages = [to_db_message(m) for m in masterlist['globals']]
        db.set_user_general_messages(db_messages)

def set_db_plugins(db, masterlist):
    for plugin in masterlist['plugins']:
        db_plugin = to_db_plugin(plugin)
        db.set_plugin_user_metadata(db_plugin)

def to_db_plugin(plugin):
    db_plugin = loot.PluginMetadata(plugin['name'])

    db_plugin.group = some_or_none(plugin, 'group')

    if 'after' in plugin:
        db_plugin.load_after_files = [to_db_file(f) for f in plugin['after']]

    if 'req' in plugin:
        db_plugin.requirements = [to_db_file(f) for f in plugin['req']]

    if 'inc' in plugin:
        db_plugin.incompatibilities = [to_db_file(f) for f in plugin['inc']]

    if 'msg' in plugin:
        db_plugin.messages = [to_db_message(m) for m in plugin['msg']]

    if 'url' in plugin:
        db_plugin.locations = [to_db_location(u) for u in plugin['url']]

    return db_plugin

def to_db_file(file):
    if isinstance(file, str):
        return loot.File(file)

    display = some_or_none(file, 'display')
    detail = some_or_none(file, 'detail')
    condition = some_or_none(file, 'condition')
    constraint = some_or_none(file, 'constraint')

    if isinstance(detail, str):
        detail = [loot.MessageContent(detail)]
    elif detail:
        raise RuntimeError('Support for multilingual file details is unimplemented!')

    return loot.File(file['name'], display, detail, condition, constraint)

def to_db_message(message):
    if message['type'] == 'say':
        message_type = loot.MessageType.Say
    elif message['type'] == 'warn':
        message_type = loot.MessageType.Warn
    elif message['type'] == 'error':
        message_type = loot.MessageType.Error

    if isinstance(message['content'], InternedString):
        contents = message['content'].value
    else:
        raise RuntimeError('Support for multilingual messages is unimplemented!')

    condition = some_or_none(message, 'condition')

    return loot.Message(message_type, contents, condition)

def to_db_location(location):
    if isinstance(location, str):
        return loot.Location(location)
    else:
        return loot.Location(location['link'], location['name'])

def some_or_none(dict: dict[str, Any], key: str):
    value = dict[key] if key in dict and dict[key] else None

    if isinstance(value, InternedString):
        return value.value

    return value

################################################################################
# End of YAML emitter code, start of conversion to LOOT metadata code
################################################################################

# PyYAML doesn't anchor/alias string scalars, only objects, so use an object
# that gets serialised as a string scalar for strings that should be
# anchored/aliased when repeated.
class InternedString:
    value: str

    def __init__(self, val):
        self.value = val

def interned_string_representer(dumper, data):
    return dumper.represent_scalar('tag:yaml.org,2002:str', data.value)

def intern_string(string: str) -> InternedString:
    return INTERNED_STRINGS.setdefault(hash(string), InternedString(string))

def is_valid_filename(filename: str):
    return '?' not in filename and '*' not in filename and '<VER>' not in filename

def expand_filename(filename: str):
    filename = filename.rstrip()

    if not is_valid_filename(filename):
        # Although the mlox docs say that "? matches any single character", the
        # parser actually treats it as "? matches any single character zero or
        # one times", see <https://github.com/mlox/mlox/blob/master/mlox/ruleParser.py#L209>
        # plox follows mlox's docs: <https://github.com/rfuzzo/plox/blob/main/src/lib.rs#L1010>
        return js_regex_escape(filename).replace('\\?', '.').replace('\\*', '.*').replace('<VER>', VER_REGEX_STR)

    return filename

# Python's re.escape() escapes some characters that that LOOT's regex engine
# doesn't accept, e.g. r"-& " becomes r"\-\&\ ".
# This is based on
# <https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/RegExp/escape>
# and
# <https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Regular_expressions/Literal_character#description>.
# Not all characters that can be escaped are escaped, because that would produce
# unreadable strings.
def js_regex_escape(value: str):
    # Omits the / as it's fine outside of character classes, which this string will be.
    SYNTAX_CHARS = '^$\\.*+?()[]{}|'
    escaped = []

    # Skip special handling of the first character, as this string won't be
    # embedded within a larger pattern (except from prefixing ^ and suffixing $)
    for c in value:
        # Skip escaping other punctuators as they're fine outside of character
        # classes.
        # Skip escaping spaces and non-ASCII white space characters as they're
        # fine.
        # Skip escaping lone surrogates as they should never appear in the input
        # data.
        if c in SYNTAX_CHARS:
            escaped.append(f'\\{c}')
        elif c == '\u000A':
            escaped.append('\\n')
        elif c == '\u000B':
            escaped.append('\\v')
        elif c == '\u000C':
            escaped.append('\\f')
        elif c == '\u000D':
            escaped.append('\\r')
        elif c == '\u0009':
            escaped.append('\\t')
        else:
            escaped.append(c)

    return ''.join(escaped)

def hex_escape(character: str):
    code = ord(character)

    return f'\\x{code:02}'

def get_plugin(masterlist, name: str):
    if name.casefold() not in masterlist['plugins']:
        masterlist['plugins'][name.casefold()] = {'name': expand_filename(name)}

    return masterlist['plugins'][name.casefold()]

def expressions_to_condition(expressions: list[Expression], known_filenames: set[str]) -> list[str]:
    return [expression_to_condition(e, known_filenames) if is_unary_expression(e) else f'({expression_to_condition(e, known_filenames)})' for e in expressions]

def expression_to_condition(expression: Expression, known_filenames: set[str]) -> str:
    # TODO: Some parentheses added for non-unary expressions could be omitted.
    match expression:
        case FileExpression(file):
            return f'file("{expand_filename(file)}")'

        case VersionExpression(operator, version, file):

            match operator:
                case Operator.LESS_THAN:
                    operator_str = '<'
                case Operator.EQUALS:
                    operator_str = '=='
                case Operator.GREATER_THAN:
                    operator_str = '>'
                case _:
                    logging.error(f'Unrecognised operator: {operator}')
                    raise RuntimeError(f'Unrecognised operator: {operator}')

            if is_valid_filename(file):
                if operator_str == '<':
                    logging.debug(f"Converting a less than version expression to a file and less than version condition to preserve mlox's behaviour when the file does not exist: {expression}")
                    return f'(file("{expand_filename(file)}") and version("{file}", "{version}", {operator_str}))'
                else:
                    return f'version("{file}", "{version}", {operator_str})'
            else:
                logging.warning(f'Converting a VER predicate that uses a filename pattern, LOOT will ignore any version in the plugin description: {expression}')

                # This is a hack to work around unsupported data that appears in <https://github.com/mlox/mlox/blob/bd5b39e4704d4574e2a7be3d0a598a686a646214/data/mlox_base.txt>. The repository is effectively abandoned, so this can't be changed upstream.
                if file == "Nevena's Twin Lamps & Slave Hunters *.esp":
                    file = "Nevena's Twin Lamps & Slave Hunters <VER>.esp"

                if '<VER>' not in file:
                    logging.error(f'Found a VER predicate that uses a filename pattern that does not contain the <VER> construct: "{expression}". There is no equivalent LOOT condition.')
                    raise RuntimeError(f'Found a VER predicate that uses a filename pattern that does not contain the <VER> construct: "{expression}". There is no equivalent LOOT condition. Matching known filenames are: {find_matching_plugin_names(file, known_filenames)}')

                return f'filename_version("{expand_filename(file)}", "{version}", {operator_str})'

        case SizeExpression(negated, size, file):
            condition = f'file_size("{file}", {size})'

            if not is_valid_filename(file):
                matches = find_matching_plugin_names(file, known_filenames)
                if matches:
                    conditions = [f'file_size("{m}", {size})' for m in matches]
                    condition = f'({' or '.join(conditions)})'

                    if add_if_missing(file, SIZE_PATTERNS):
                        logging.warning(f'A SIZE predicate uses a filename that contains special characters: "{expression}". It has been expanded to check any of the following matching filenames: {matches}')
                elif add_if_missing(file, SIZE_PATTERNS):
                    logging.error(f'A SIZE predicate uses a filename that contains special characters: "{expression}". No matching filenames were found, and the equivalent expression will never be true at runtime.')

            if negated:
                return 'not ' + condition
            else:
                return condition

        case DescriptionExpression(negated, regex, file):
            condition = f'description_contains("{file}", "{regex}")'

            if not is_valid_filename(file):
                matches = find_matching_plugin_names(file, known_filenames)
                if matches:
                    conditions = [f'description_contains("{m}", "{regex}")' for m in matches]
                    condition = f'({' or '.join(conditions)})'

                    if add_if_missing(file, DESC_PATTERNS):
                        logging.warning(f'A DESC predicate uses a filename that contains special characters: "{expression}". It has been expanded to check any of the following matching filenames: {matches}')
                elif add_if_missing(file, DESC_PATTERNS):
                    logging.error(f'A DESC predicate uses a filename that contains special characters: "{expression}". No matching filenames were found, and the equivalent expression will never be true at runtime.')

            if negated:
                return 'not ' + condition
            else:
                return condition

        case NotExpression(expressions):
            conditions = expressions_to_condition(expressions, known_filenames)

            if len(conditions) == 1:
                if conditions[0].startswith('not '):
                    return conditions[0][4:]
                else:
                    return f'not {conditions[0]}'
            else:
                joined_conditions = ' and '.join(conditions)
                return f'not ({joined_conditions})'

        case AnyExpression(expressions):
            conditions = expressions_to_condition(expressions, known_filenames)
            return ' or '.join(conditions)

        case AllExpression(expressions):
            conditions = expressions_to_condition(expressions, known_filenames)
            return ' and '.join(conditions)

        case _:
            logging.error(f'Unrecognised expression type: {type(expression)}')
            raise RuntimeError('Unrecognised expression type')

def expression_files(expression: Expression, ignore_file_expressions=False) -> list[str]:
    match expression:
        case FileExpression(file):
            return [] if ignore_file_expressions else [file]
        case VersionExpression() | SizeExpression() | DescriptionExpression():
            return [expression.file]
        case AnyExpression() | AllExpression():
            return [f for e in expression.expressions for f in expression_files(e, ignore_file_expressions)]
        case NotExpression(expressions):
            return [f for e in expressions for f in expression_files(e, not ignore_file_expressions)]

def convert_near_start(rule: NearStartRule, masterlist):
    for entry in rule.plugins:
        plugin = get_plugin(masterlist, entry)
        plugin['group'] = GROUP_NEAR_START

    return True

def convert_near_end(rule: NearEndRule, masterlist):
    for entry in rule.plugins:
        plugin = get_plugin(masterlist, entry)
        plugin['group'] = GROUP_NEAR_END

    return True

def convert_order(rule: OrderRule, masterlist, known_filenames: set[str]):
    if len(rule.plugins) > 1:
        plugins = []
        for index, entry in enumerate(rule.plugins):
            plugins.append(entry)

            if not is_valid_filename(entry):
                matches = find_matching_plugin_names(entry, known_filenames)
                if matches:
                    plugins.extend(matches)

                    if add_if_missing(entry, AFTER_PATTERNS):
                        logging.warning(f'The plugin name "{entry}" contains special characters and so is not valid as an "after" filename. It has been retained and is followed by entries for the following matching filenames: {matches}')
                elif add_if_missing(entry, AFTER_PATTERNS):
                    logging.warning(f'The plugin name "{entry}" contains special characters and so is not valid as an "after" filename. No matching filenames were found, and its "after" entry will never match any files at runtime.')

        for index, entry in enumerate(plugins):
            if index == 0:
                continue

            plugin = get_plugin(masterlist, entry)
            if 'after' not in plugin:
                plugin['after'] = []

            plugin['after'].extend(plugins[:index])

            # Deduplicate entries
            plugin['after'] = deduplicate(plugin['after'])

    return True

# Taken from <https://github.com/loot/loot/blob/0.24.1/src/gui/state/game/helpers.cpp#L113> with the following characters removed as they don't seem to trigger special behaviour: "$%'(),/:;?@^{}.
# Other characters have also been removed but are dealt with in more specific context-aware regexes.
MARKDOWN_ASCII_PUNCTUATION_REGEX = re.compile(r"([*<\[\\`|])")
# <https://spec.commonmark.org/0.31.2/#entity-and-numeric-character-references>
ENTITY_REFERENCE = re.compile(r'(&.+;)')
# <https://spec.commonmark.org/0.31.2/#atx-headings>
ATX_HEADING_OPENING = re.compile(r'((?:^|\n)[ ]{0,3})(#{1,6})(?=[ \t]+|\n)')
# <https://spec.commonmark.org/0.31.2/#setext-headings>
SETEXT_HEADING = re.compile(r'((?:^|\n)[ ]{0,3})(-+|=+)(?=[ \t]*(?:\n|$))')
# <https://spec.commonmark.org/0.31.2/#list-items>
BULLET_LIST_MARKER = re.compile(r'(^|[^A-Za-z0-9")/!\]\' ] +)([-*+])(?=\s)')
ORDERED_LIST_MARKER = re.compile(r'((?:^|[^A-Za-z,\]]\s+)\d{1,9})([.)])(?=\s)')
# <https://spec.commonmark.org/0.31.2/#links>
LINK_TEXT_CLOSE = re.compile(r'(\])(?=\(|\[)')
# <https://spec.commonmark.org/0.31.2/#fenced-code-blocks>
FENCED_CODE_BLOCK = re.compile(r'(~{3}|`{3})')
# <https://spec.commonmark.org/0.31.2/#block-quotes>
BLOCK_QUOTE = re.compile(r'((?:^|\n)[ ]{0,3})(>)')

def escape_markdown_ascii_punctuation(text: str):
    text = MARKDOWN_ASCII_PUNCTUATION_REGEX.sub(r'\\\1', text)
    text = ENTITY_REFERENCE.sub(r'\\\1', text)
    text = ATX_HEADING_OPENING.sub(r'\1\\\2', text)
    text = SETEXT_HEADING.sub(r'\1\\\2', text)
    text = BULLET_LIST_MARKER.sub(r'\1\\\2', text)
    text = ORDERED_LIST_MARKER.sub(r'\1\\\2', text)
    text = LINK_TEXT_CLOSE.sub(r'\\\1', text)
    text = FENCED_CODE_BLOCK.sub(r'\\\1', text)
    text = BLOCK_QUOTE.sub(r'\1\\\2', text)

    underscore_count = text.count('_')
    if underscore_count > 1:
        # It's OK to leave one underscore unescaped as it doesn't have special
        # meaning on its own.
        text = text.replace('_', '\\_', underscore_count - 1)

    return text

def to_loot_message(message: Message, condition: str | None = None):
    match = HIDE_TAGS_REGEX.search(message.text)
    if match:
        logging.info(f'Found a message containing <hide></hide> tags, removing them and their contents: "{message.text}"')
        text = message.text[:match.start()] + message.text[match.end():]
    else:
        text = message.text

    loot_message = {
        'type': 'say',
        'content': intern_string(escape_markdown_ascii_punctuation(text))
    }

    if message.highlight == Highlight.RED:
        loot_message['type'] = 'error'
    elif message.highlight == Highlight.YELLOW:
        loot_message['type'] = 'warn'
    elif message.highlight == Highlight.BLUE and add_if_missing(loot_message['content'].value, BLUE_MESSAGE_CONTENT):
        logging.info(f"LOOT does not support message highlighting equivalent to mlox's blue highlighting, the following message will be treated as not highlighted: \"{text}\"")

    if condition:
        loot_message['condition'] = intern_string(condition)

    message_hash = hash((
        loot_message['type'],
        loot_message['content'],
        loot_message['condition'] if 'condition' in loot_message else None,
    ))

    return INTERNED_MESSAGES.setdefault(message_hash, loot_message)

def append_message(plugin, message):
    if 'msg' not in plugin:
        plugin['msg'] = []

    plugin['msg'].append(message)

def is_any_files_expression(expression: Expression):
    return isinstance(expression, AnyExpression) and all(isinstance(e, FileExpression) for e in expression.expressions)

def is_all_files_expression(expression: Expression):
    return isinstance(expression, AllExpression) and all(isinstance(e, FileExpression) for e in expression.expressions)

def is_any_unary_expressions_expression(expression: Expression):
    return isinstance(expression, AnyExpression) and all(is_unary_expression(e) for e in expression.expressions)

def is_all_unary_expressions_expression(expression: Expression):
    return isinstance(expression, AllExpression) and all(is_unary_expression(e) for e in expression.expressions)

def is_unary_expression(expression: Expression):
    match expression:
        case FileExpression() | VersionExpression() | SizeExpression() | DescriptionExpression():
            return True
        case _:
            return False

def append_message_to_files(message, files: list[str], processed_files: set[str]):
    for file in files:
        if file in processed_files:
            continue

        plugin = get_plugin(masterlist, file)
        append_message(plugin, message)

        processed_files.add(file)

def flatten_expressions(expressions: list[Expression], class_to_flatten):
    flattened = []
    for expression in expressions:
        if isinstance(expression, class_to_flatten):
            flattened.extend(flatten_expressions(expression.expressions, class_to_flatten))
        else:
            flattened.append(expression)

    return flattened

def convert_note(rule: NoteRule, masterlist, known_filenames: set[str]):
    if not rule.message:
        # For some reason messages aren't required for notes.
        logging.warning(f'Encountered a note rule with no message, ignoring it: {rule}')
        return False

    processed_files: set[str] = set()
    # If there are multiple expressions then the message is shown when any of them are true, i.e. they're effectively OR'ed together.
    if not rule.expressions:
        # Treat as a global message
        logging.debug(f'Treating note without expressions as a global message: {rule}')
        masterlist['globals'].append(to_loot_message(rule.message))
    else:
        # The rule's expressions check for more than just files' existence, check each one by one.
        for expression in flatten_expressions(rule.expressions, AnyExpression):
            if isinstance(expression, FileExpression):
                # The simple case, just add the message to each plugin.
                # In case a condition was added in a previous loop.
                message = to_loot_message(rule.message)
                append_message_to_files(message, [expression.file], processed_files)

            elif is_unary_expression(expression):
                # Add the message to the plugin, but with a condition
                condition = expression_to_condition(expression, known_filenames)
                message = to_loot_message(rule.message, condition)

                # Don't check or record the file as processed as the message is dependent on a condition, and may be repeated for the same file but with other conditions.
                plugin = get_plugin(masterlist, expression.file)
                append_message(plugin, message)

            else:
                files = expression_files(expression)
                # TODO: This condition could be made simpler for files that appear only in file expressions by removing that expression when adding the message to that plugin.
                condition = expression_to_condition(expression, known_filenames)
                message = to_loot_message(rule.message, condition)

                append_message_to_files(message, files, processed_files)

    return True

def to_file_metadata(filename, detail, condition, constraint = None):
    if not detail and not condition and not constraint:
        return filename

    file = {
        'name': filename
    }

    if detail:
        file['detail'] = intern_string(detail)

    if condition:
        file['condition'] = intern_string(condition)

    if constraint:
        file['constraint'] = intern_string(constraint)

    file_hash = hash((
        file['name'],
        file['detail'] if 'detail' in file else None,
        file['condition'] if 'condition' in file else None,
        file['constraint'] if 'constraint' in file else None
    ))

    return INTERNED_FILES.setdefault(file_hash, file)

def convert_requirement(dependent_plugins, detail_text, consequent_expression, known_filenames: set[str], shared_condition = None):
    if is_unary_expression(consequent_expression):
        constraint = None if isinstance(consequent_expression, FileExpression) else expression_to_condition(consequent_expression, known_filenames)

        if is_valid_filename(consequent_expression.file):
            req_files = [consequent_expression.file]
        else:
            matches = find_matching_plugin_names(consequent_expression.file, known_filenames)
            if matches:
                req_files = matches

                if add_if_missing(consequent_expression.file, REQ_PATTERNS):
                    logging.warning(f'The filename "{consequent_expression.file}" contains special characters and so is not valid as a "req" filename. It has been replaced with the following matching filenames: {matches}')
            else:
                if add_if_missing(consequent_expression.file, REQ_PATTERNS):
                    logging.warning(f'The filename "{consequent_expression.file}" contains special characters and so is not valid as a "req" filename. No matching filenames were found, so its "req" entry has been skipped.')
                return True

        for plugin in dependent_plugins:
            if 'req' not in plugin:
                plugin['req'] = []

            for req_file in req_files:
                file_metadata = to_file_metadata(req_file, detail_text, shared_condition, constraint)

                plugin['req'].append(file_metadata)

        return True
    elif isinstance(consequent_expression, AnyExpression):
        # The dependency is satisfied if any of the consequent files are present.
        # That's equivalent to a condition that each of the consequent files are individually required when none of the other consequent files are present.
        # TODO: This condition could be different for each file, omitting it from the condition if it's only present in a file expression.
        condition = expression_to_condition(NotExpression([consequent_expression]), known_filenames)
        if shared_condition:
            condition = f'({shared_condition}) and ({condition})'

        flattened_expressions = flatten_expressions(consequent_expression.expressions, AllExpression)

        failed = False
        for expression in flattened_expressions:
            if convert_requirement(dependent_plugins, detail_text, expression, known_filenames, condition):
                pass
            else:
                failed = True

        return not failed

    return False

def convert_requires(rule: RequiresRule, masterlist, known_filenames: set[str]):
    dependent_plugins = [get_plugin(masterlist, f) for f in expression_files(rule.dependent)]

    detail = escape_markdown_ascii_punctuation(rule.message.text) if rule.message else None

    if is_unary_expression(rule.dependent) or is_any_unary_expressions_expression(rule.dependent) or isinstance(rule.dependent, AllExpression):
        # If the dependent expression is an AnyExpression, then each of the sub-expressions can be handled as if they appeared in separate Requires rules.
        dependent_expressions = flatten_expressions([rule.dependent], AnyExpression)

        # If the consequent expression is an AllExpression, then that means its sub-expressions must all be true, or an error message will be displayed. That's equivalent to saying that an error message should be displayed if any one sub-expression is false, so each sub-expression can be treated as an independent requirement.
        consequent_expressions = flatten_expressions([rule.consequent], AllExpression)

        failed = False
        for dependent_expression in dependent_expressions:
            if isinstance(dependent_expression, NotExpression):
                failed = True
                continue
            elif isinstance(dependent_expression, FileExpression):
                condition = None
            else:
                condition = expression_to_condition(dependent_expression, known_filenames)

            for consequent_expression in consequent_expressions:
                if isinstance(consequent_expression, NotExpression):
                    # The dependent plugin(s) requires that the NotExpression's sub-expressions are not all true.
                    # That seems indistinguishable from a conflict when they are all true.
                    if len(consequent_expression.expressions) > 1:
                        failed = True
                        continue

                    conflict_expressions = [rule.dependent, consequent_expression.expressions[0]]

                    logging.debug(f'Encountered a requires rule with a NOT expression, attempting to convert it as a conflict rule: {rule}')
                    if not convert_conflict(ConflictRule(rule.message, conflict_expressions), masterlist, known_filenames):
                        failed = True

                elif not convert_requirement(dependent_plugins, detail, consequent_expression, known_filenames, condition):
                    failed = True

        return not failed

    return False

def is_single_file_not_expression(expression: Expression):
    return isinstance(expression, NotExpression) and len(expression.expressions) == 1 and isinstance(expression.expressions[0], FileExpression)

def get_condition(expression: Expression, other_expression: Expression, known_filenames: set[str]):
    expression_is_file = isinstance(expression, FileExpression)
    other_expression_is_file = isinstance(other_expression, FileExpression)

    if expression_is_file and other_expression_is_file:
        return None
    elif expression_is_file:
        return expression_to_condition(other_expression, known_filenames)
    elif other_expression_is_file:
        return expression_to_condition(expression, known_filenames)
    else:
        return expression_to_condition(AllExpression([expression, other_expression]), known_filenames)

def append_inc(masterlist, filename, other_filename, detail, condition, known_filenames: set[str]):
    # It doesn't matter which file is treated as the plugin and which as the incompatibility, but only add the incompatibility in one direction to avoid bloat/noise, as it only needs to appear under one plugin.

    # With that in mind, try to use a file that doesn't contain special characters as the inc file.
    if is_valid_filename(other_filename):
        plugin_filename = filename
        inc_filename = other_filename
    else:
        plugin_filename = other_filename
        inc_filename = filename

    plugin = get_plugin(masterlist, plugin_filename)
    if 'inc' not in plugin:
        plugin['inc'] = []

    if is_valid_filename(inc_filename):
        inc_files = [inc_filename]
    else:
        matches = find_matching_plugin_names(inc_filename, known_filenames)
        if matches:
            inc_files = matches

            if add_if_missing(inc_filename, INC_PATTERNS):
                logging.warning(f'The filename "{inc_filename}" contains special characters and so is not valid as an "inc" filename. It has been replaced with the following matching filenames: {matches}')
        else:
            inc_files = [inc_filename]

            if add_if_missing(inc_filename, INC_PATTERNS):
                logging.warning(f'The filename "{inc_filename}" contains special characters and so is not valid as a "inc" filename. No matching filenames were found, and its "inc" entry will never match any files at runtime.')

    for inc_file in inc_files:
        file_metadata = to_file_metadata(inc_file, detail, condition)
        plugin['inc'].append(file_metadata)

def convert_conflict(rule: ConflictRule, masterlist, known_filenames: set[str]):
    if not rule.expressions:
        logging.error(f'Encountered a conflict rule with no expressions: {rule}')
        raise RuntimeError('Encountered a conflict rule with no expressions')

    elif len(rule.expressions) == 1 and not rule.message:
        logging.error(f'Encountered a conflict rule with no message and only one expression: {rule}')
        raise RuntimeError('Encountered a conflict rule with only one expression and no message')

    elif len(rule.expressions) == 1:
        logging.debug(f'Encountered a conflict rule with a message and only one expression, attempting to convert it as a note rule: {rule}')

        return convert_note(NoteRule(rule.message, rule.expressions), masterlist, known_filenames)

    detail = escape_markdown_ascii_punctuation(rule.message.text) if rule.message else None

    # For each file in each expression, add all the files in all other expressions as inc entries.
    for index, expression in enumerate(rule.expressions):
        expressions = flatten_expressions([expression], AnyExpression)

        other_expressions = [e for i, e in enumerate(rule.expressions) if i != index]
        other_expressions = flatten_expressions(other_expressions, AnyExpression)

        failed = False
        for expression in expressions:
            files = expression_files(expression)

            expression_is_not = isinstance(expression, NotExpression)

            if expression_is_not and not is_single_file_not_expression(expression):
                failed = True
                continue

            for other_expression in other_expressions:

                other_expression_is_not = isinstance(other_expression, NotExpression)
                if other_expression_is_not and not is_single_file_not_expression(other_expression):
                    if len(other_expression.expressions) == 1 and is_any_files_expression(other_expression.expressions[0]):
                        logging.debug('Encountered a conflict rule expression with a NOT(ANY([File])) expression, turning it into an ALL([NOT(File)]) expression')
                        other_expression = AllExpression([NotExpression([e]) for e in other_expression.expressions[0].expressions])
                    else:
                        failed = True
                        continue

                other_files = expression_files(other_expression)

                condition = get_condition(expression, other_expression, known_filenames)

                if not files and not other_files:
                    logging.warning(f'Encountered a conflict rule with a pair of expressions that produce no files: {rule}')
                    failed = True

                elif not files or not other_files:
                    if not files:
                        dependent_expression = other_expression
                        consequent_expression = expression
                    else:
                        dependent_expression = expression
                        consequent_expression = other_expression

                    if is_single_file_not_expression(consequent_expression):
                        consequent_expression = consequent_expression.expressions[0]
                    elif isinstance(consequent_expression, AllExpression) and all(is_single_file_not_expression(e) for e in consequent_expression.expressions):
                        consequent_expression = AnyExpression([
                            ee for e in consequent_expression.expressions for ee in e.expressions
                        ])
                    else:
                        failed = True
                        continue

                    logging.debug(f'Encountered a conflict rule with an expression that produced no files, attempting to convert it as a requires rule: {rule}')
                    if not convert_requires(RequiresRule(rule.message, dependent_expression, consequent_expression), masterlist, known_filenames):
                        failed = True

                for file in files:
                    for other_file in other_files:
                        append_inc(masterlist, file, other_file, detail, condition, known_filenames)

        return not failed

    return True

def convert_patch(rule: PatchRule, masterlist, known_filenames: set[str]):
    # Patches are just reciprocal requires rules.
    # The patch(es) require the original file(s).
    patch_requires = convert_requires(RequiresRule(rule.message, rule.patch, rule.original), masterlist, known_filenames)

    # The original file(s) also require(s) the patch(es).
    original_requires = convert_requires(RequiresRule(rule.message, rule.original, rule.patch), masterlist, known_filenames)

    return patch_requires and original_requires

def convert_rule(rule: Rule, masterlist, known_filenames: set[str]):
    match rule:
        case NearStartRule():
            return convert_near_start(rule, masterlist)
        case NearEndRule():
            return convert_near_end(rule, masterlist)
        case OrderRule():
            return convert_order(rule, masterlist, known_filenames)
        case NoteRule():
            return convert_note(rule, masterlist, known_filenames)
        case RequiresRule():
            return convert_requires(rule, masterlist, known_filenames)
        case ConflictRule():
            return convert_conflict(rule, masterlist, known_filenames)
        case PatchRule():
            return convert_patch(rule, masterlist, known_filenames)
        case _:
            logging.error(f'Unrecognised rule type: {type(rule)}')
            raise RuntimeError(f'Unrecognised rule type: {type(rule)}')

def convert_rules(rules: list[Rule], masterlist, known_filenames: set[str]):
    unconverted_rules = []
    for rule in rules:
        if not convert_rule(rule, masterlist, known_filenames):
            logging.warning(f'Failed to convert rule: {rule}')
            unconverted_rules.append(rule)

    return unconverted_rules

def read_plugins_index(input) -> list[ModPlugin]:
    reader = csv.DictReader(input, delimiter='\t')

    return [ModPlugin(
            row["modId"],
            row['modName'] if 'modName' in row else None,
            row["fileName"],
            row["url"]
        ) for row in reader]

def add_urls_to_masterlist(masterlist_plugins, plugins_index: list[ModPlugin]):
    # Create a map for much faster lookups for non-regex plugin entries.
    plugins_index_map = {}
    for entry in plugins_index:
        folded_name = entry.plugin_name.casefold()
        if folded_name in plugins_index_map:
            plugins_index_map[folded_name].append(entry)
        else:
            plugins_index_map[folded_name] = [entry]

    for plugin in masterlist_plugins:
        urls = {}
        for mod_plugin in find_matching_plugins(plugin["name"], plugins_index, plugins_index_map):
            if mod_plugin.url not in urls:
                if mod_plugin.mod_name:
                    urls[mod_plugin.url] = {
                        'link': mod_plugin.url,
                        'name': mod_plugin.mod_name
                    }
                else:
                    urls[mod_plugin.url] = mod_plugin.url
            elif mod_plugin.mod_name:
                urls[mod_plugin.url] = {
                    'link': mod_plugin.url,
                    'name': mod_plugin.mod_name
                }

        if urls:
            plugin["url"] = sorted(urls.values(), key=lambda x: x if isinstance(x, str) else x['link'])

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('-i', '--input-path', default=Path.cwd() / 'mlox' / 'mlox_base.txt')
    parser.add_argument('-o', '--output-path')
    parser.add_argument('-l', '--log-path', default=Path(__file__).with_suffix('.log'))
    parser.add_argument('-s', '--log-severity', default='info', choices=['debug', 'info', 'warning', 'error'])
    parser.add_argument('-p', '--plugins-index-paths', action='append', default=[])
    args = parser.parse_args()

    logging.basicConfig(filename=args.log_path, filemode='w', level=args.log_severity.upper())
    logging.getLogger().addHandler(logging.StreamHandler())

    plugins_index = []
    for plugins_index_path in args.plugins_index_paths:
        with open(plugins_index_path, encoding='utf8') as input:
            plugins_index.extend(read_plugins_index(input))

    known_filenames = set(p.plugin_name for p in plugins_index)

    rules = []
    with open(args.input_path, encoding='utf8') as input:
        rules = read_rules(input)

    known_filenames.update(get_filenames(rules))

    masterlist = {
        'groups': [
            {
                'name': GROUP_NEAR_START
            },
            {
                'name': 'default',
                'after': [GROUP_NEAR_START]
            },
            {
                'name': GROUP_NEAR_END,
                'after': ['default']
            }
        ],
        'globals': [],
        'plugins': {} # This is actually a list in the real masterlist, but it's easier to work here with as a set until everything has been added.
    }

    unconverted_rules = convert_rules(rules, masterlist, known_filenames)

    counters = {
        'note': 0,
        'requires': 0,
        'conflict': 0,
        'patch': 0
    }

    logging.info(f'Rule count: {len(rules)}, conversion failure count: {len(unconverted_rules)}')

    for rule in unconverted_rules:
        match rule:
            case NoteRule():
                counters['note'] += 1
            case RequiresRule():
                counters['requires'] += 1
            case ConflictRule():
                counters['conflict'] += 1
            case PatchRule():
                counters['patch'] += 1

    if unconverted_rules:
        logging.warning(f'Failures by rule type: {counters}')

    # Now convert the plugins to a list.
    masterlist['plugins'] = list(v for v in masterlist['plugins'].values())

    add_urls_to_masterlist(masterlist['plugins'], plugins_index)

    logging.info(f'There are {len(masterlist['globals'])} globals and {len(masterlist['plugins'])} plugin entries in the masterlist')

    output_path = args.output_path if args.output_path else str(args.input_path) + ".yaml"

    write_masterlist(masterlist, output_path)
