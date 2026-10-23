"""Managed, immutable image assets and portable note backup snapshots."""
from __future__ import annotations
import base64
import datetime
from collections import OrderedDict
from contextlib import closing
import html
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import uuid
from urllib.parse import unquote, urlparse

from PyQt6.QtGui import QImage

IMAGE_SRC = re.compile(r'(<img\b[^>]*?\bsrc\s*=\s*)([\"\'])(.*?)(\2)', re.I | re.S)


class ImageCache:
    """Decoded images by file path, least recently used dropped first above max_bytes.

    The note view asks for every image on each paint; decoding a large PNG takes ~100 ms.
    Entries are checked against the file's mtime and size, so a changed file is read again.
    """

    def __init__(self, max_bytes=192 * 1024 * 1024):
        self.max_bytes = max_bytes
        self._entries = OrderedDict()  # path -> ((mtime_ns, size), QImage)
        self._bytes = 0

    def load(self, path):
        """QImage of the file at path (null when missing or not an image)."""
        path = os.path.abspath(str(path))
        try:
            stat = os.stat(path)
        except OSError:
            return QImage()
        stamp = (stat.st_mtime_ns, stat.st_size)
        entry = self._entries.get(path)
        if entry is not None and entry[0] == stamp:
            self._entries.move_to_end(path)
            return QImage(entry[1])  # shallow copy: a caller painting on it detaches
        if entry is not None:
            self._drop(path)
        image = QImage(path)
        if image.isNull():
            return image
        size = image.sizeInBytes()
        if size <= self.max_bytes:
            self._entries[path] = (stamp, image)
            self._bytes += size
            while self._bytes > self.max_bytes:
                self._drop(next(iter(self._entries)))
        return QImage(image)

    def _drop(self, path):
        _stamp, image = self._entries.pop(path)
        self._bytes -= image.sizeInBytes()

    def clear(self):
        self._entries.clear()
        self._bytes = 0


IMAGE_CACHE = ImageCache()


class NoteAssets:
    def __init__(self, directory):
        self.directory = Path(directory)

    def resolve(self, source):
        source = html.unescape(source)
        if source.startswith('file:'):
            return Path(unquote(urlparse(source).path))
        path = Path(source)
        return path if path.is_absolute() else self.directory.parent / path

    def image(self, source):
        try:
            if source.startswith('data:image/'):
                header, data = source.split(',', 1)
                raw = base64.b64decode(data, validate=True) if ';base64' in header else unquote(data).encode('latin1')
                return QImage.fromData(raw)
            if not Path(source).is_absolute() and urlparse(source).scheme not in ('', 'file'):
                return QImage()
            return IMAGE_CACHE.load(self.resolve(source))
        except (ValueError, UnicodeError):
            return QImage()

    def store(self, image):
        if image.isNull():
            raise ValueError('The image could not be decoded.')
        self.directory.mkdir(parents=True, exist_ok=True)
        name = uuid.uuid4().hex + '.png'
        target = self.directory / name
        # Never replace an existing asset: crop, paste and imports get independent files.
        if not image.save(str(target), 'PNG'):
            raise OSError('Could not write the image to the attachments folder.')
        return self.directory.name + '/' + name

    def import_html(self, content):
        imported = {}
        def replace(match):
            source = html.unescape(match.group(3))
            if source not in imported:
                image = self.image(source)
                if image.isNull():
                    raise ValueError('An image in the pasted content could not be imported. Paste text only or copy the image itself.')
                imported[source] = self.store(image)
            return match.group(1) + '"' + html.escape(imported[source], quote=True) + '"'
        return IMAGE_SRC.sub(replace, content)

    def embed_html(self, content):
        def replace(match):
            source = html.unescape(match.group(3))
            if source.startswith('data:image/'):
                return match.group(0)
            image = self.image(source)
            if image.isNull():
                return match.group(0)
            from PyQt6.QtCore import QByteArray, QBuffer, QIODevice
            data = QByteArray()
            buffer = QBuffer(data)
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            image.save(buffer, 'PNG')
            return match.group(1) + '"data:image/png;base64,' + base64.b64encode(bytes(data)).decode('ascii') + '"'
        return IMAGE_SRC.sub(replace, content)


def _backup_name_pattern(database_stem):
    """Matches backup files and folders of one database: <stem>.backup-… or <stem>.full-…"""
    return re.compile(rf'^{re.escape(database_stem)}\.(?:backup|full)-\d{{8}}-\d{{6}}')


def _latest_assets_dir(backup_path, database_stem):
    """The .assets directory of the most recent earlier backup of the same database, if any."""
    pattern = _backup_name_pattern(database_stem)
    try:
        earlier = [entry for entry in backup_path.parent.iterdir()
                   if entry.name.endswith('.assets') and pattern.match(entry.name) and entry.is_dir()]
        return max(earlier, key=lambda entry: entry.stat().st_mtime) if earlier else None
    except OSError:
        return None


def _copy_or_link(source, target, earlier):
    """Hard-link target to the same image in an earlier backup, else copy it.

    Assets are never changed in place, so an earlier copy with the same size and modification
    time is the same file; linking it costs no disk space. The backup layout stays the same.
    """
    if earlier is not None:
        try:
            a, b = os.stat(source), os.stat(earlier)
            if a.st_size == b.st_size and a.st_mtime_ns == b.st_mtime_ns:
                os.link(earlier, target)
                return
        except OSError:
            pass  # not there, or the filesystem has no hard links
    shutil.copy2(source, target)


class DatabaseCheckFailed(Exception):
    """The snapshot failed SQLite's integrity check, so it was not published."""


def _checked_snapshot(connection, destination):
    """Copy the live database into destination, then check the copy for damage.

    The online backup copies pages as they are, so damage in the live database shows up in
    the copy. Raises DatabaseCheckFailed when PRAGMA quick_check reports anything but 'ok'.
    """
    connection.backup(destination)
    destination.execute('PRAGMA journal_mode=DELETE')
    _quick_check(destination)


def _quick_check(connection):
    problems = [row[0] for row in connection.execute('PRAGMA quick_check(5)')]
    if problems != ['ok']:
        raise DatabaseCheckFailed('; '.join(str(problem) for problem in problems))


def backup_database(connection, backup_path, check=True):
    """SQLite snapshot of the database only, published only when complete.

    With check (the default) the snapshot must also pass the integrity check. The copy made
    before a restore skips it: a damaged database is exactly what gets restored over."""
    backup_path = Path(backup_path)
    handle, temporary = tempfile.mkstemp(dir=backup_path.parent, prefix='.notes-backup-',
                                         suffix=backup_path.suffix)
    os.close(handle)
    try:
        with closing(sqlite3.connect(temporary)) as destination:
            if check:
                _checked_snapshot(connection, destination)
            else:
                connection.backup(destination)
                destination.execute('PRAGMA journal_mode=DELETE')
        os.replace(temporary, backup_path)
    except BaseException:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


def restore_database(backup_path, connection):
    """Replace everything in connection's database with the backup, after checking the backup.

    Uses SQLite's online backup in reverse, as one transaction: if it fails, the open
    database is left as it was. Raises DatabaseCheckFailed for a damaged backup."""
    uri = Path(backup_path).resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as source:
        _quick_check(source)
        if connection.in_transaction:
            connection.commit()
        source.backup(connection)


def _referenced_images(connection, database_path):
    """Image files the database refers to: note and template HTML, and attachment rows."""
    folder = Path(database_path).parent
    attach_dir = folder / (Path(database_path).stem + '_attachments')

    def local_file(source, bare_dir):
        source = html.unescape(source or '').strip()
        if not source or source.startswith('data:'):
            return None
        parsed = urlparse(source)
        if parsed.scheme == 'file':
            file = Path(unquote(parsed.path))
        elif Path(source).is_absolute():
            file = Path(source)
        elif parsed.scheme:
            return None  # stored on the web
        elif bare_dir is not None and '/' not in source and '\\' not in source:
            file = bare_dir / source  # attachment rows may hold a bare file name
        else:
            file = folder / source.replace('\\', '/')
        return Path(os.path.normpath(file))

    files = set()
    for table in ('notes', 'note_templates'):
        for (content,) in connection.execute(f'SELECT content_html FROM {table}'):
            for match in IMAGE_SRC.finditer(content or ''):
                file = local_file(match.group(3), None)
                if file:
                    files.add(file)
    for (filename,) in connection.execute('SELECT filename FROM attachments'):
        file = local_file(filename, attach_dir)
        if file:
            files.add(file)
    return files


def restore_missing_images(connection, database_path, asset_dirs):
    """Copy back images the database refers to that are missing on disk.

    Each one is taken from the first full backup .assets folder (in the order given) that
    has it; existing files are never overwritten. Returns (restored, still_missing) paths."""
    folder = Path(database_path).parent.resolve()
    restored, missing = [], []
    for file in sorted(_referenced_images(connection, database_path)):
        if file.exists():
            continue
        try:
            relative = file.resolve().relative_to(folder)
        except ValueError:
            missing.append(file)  # outside the database folder: full backups rename these
            continue
        for assets in asset_dirs:
            source = Path(assets) / relative
            if source.is_file():
                file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, file)
                restored.append(file)
                break
        else:
            missing.append(file)
    return restored, missing


def select_backups_to_delete(entries, now, keep_recent=10, keep_days=8):
    """Which backups to remove: keep the newest keep_recent, plus the newest one of each of the
    last keep_days calendar days (today included). entries is a list of (path, datetime)."""
    ordered = sorted(entries, key=lambda entry: entry[1], reverse=True)
    keep = {path for path, _ in ordered[:keep_recent]}
    first_day = now.date() - datetime.timedelta(days=keep_days - 1)
    seen_days = set()
    for path, stamp in ordered:
        day = stamp.date()
        if first_day <= day <= now.date() and day not in seen_days:
            seen_days.add(day)
            keep.add(path)
    return [path for path, _ in ordered if path not in keep]


def backup_notes(connection, database_path, backup_path):
    """SQLite snapshot plus assets and a restore manifest, published only after completion.

    Restore the database using manifest.database_name and copy the attachment folders
    from the .assets directory beside it. Asset paths are taken from snapshot HTML.
    """
    backup_path = Path(backup_path)
    database_path = Path(database_path)
    previous_assets = _latest_assets_dir(backup_path, database_path.stem)
    with tempfile.TemporaryDirectory(dir=backup_path.parent, prefix='.notes-backup-') as work:
        snapshot = Path(work) / backup_path.name
        assets = Path(work) / 'assets'
        assets.mkdir()
        with closing(sqlite3.connect(snapshot)) as destination:
            _checked_snapshot(connection, destination)
            copied = {}
            def copy_source(match):
                source = html.unescape(match.group(3))
                if source.startswith('data:'):
                    return match.group(0)
                if source not in copied:
                    parsed = urlparse(source)
                    if not Path(source).is_absolute() and parsed.scheme not in ('', 'file'):
                        raise ValueError('Backup contains an image that is not stored locally.')
                    file = Path(unquote(parsed.path)) if parsed.scheme == 'file' else Path(source)
                    file = file if file.is_absolute() else database_path.parent / file
                    file = file.resolve()
                    try:
                        relative = file.relative_to(database_path.parent.resolve())
                    except ValueError:
                        relative = Path(database_path.stem + '_attachments') / (uuid.uuid4().hex + file.suffix)
                    target = assets / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    _copy_or_link(file, target, previous_assets / relative if previous_assets else None)
                    copied[source] = relative.as_posix()
                return match.group(1) + '"' + html.escape(copied[source], quote=True) + '"'
            for table, key in (('notes', 'task_id'), ('note_templates', 'id')):
                for identity, content in list(destination.execute(f'SELECT {key}, content_html FROM {table}')):
                    content = IMAGE_SRC.sub(copy_source, content or '')
                    destination.execute(f'UPDATE {table} SET content_html=? WHERE {key}=?', (content, identity))
            for old, new in copied.items():
                destination.execute('UPDATE attachments SET filename=? WHERE filename=?', (new, old))
            destination.commit()
        (assets / 'restore.json').write_text(json.dumps({
            'database_name': database_path.name,
            'instructions': 'Copy the backup database using database_name, and copy the attachment folders beside it.',
        }, indent=2), encoding='utf-8')
        os.replace(assets, str(backup_path) + '.assets')
        try:
            os.replace(snapshot, backup_path)
        except Exception:
            shutil.rmtree(str(backup_path) + '.assets')
            raise
