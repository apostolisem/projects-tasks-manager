"""Managed, immutable image assets and portable note backup snapshots."""
from __future__ import annotations
import base64
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
            return QImage(str(self.resolve(source)))
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


def backup_notes(connection, database_path, backup_path):
    """SQLite snapshot plus assets and a restore manifest, published only after completion.

    Restore the database using manifest.database_name and copy the attachment folders
    from the .assets directory beside it. Asset paths are taken from snapshot HTML.
    """
    backup_path = Path(backup_path)
    database_path = Path(database_path)
    with tempfile.TemporaryDirectory(dir=backup_path.parent, prefix='.notes-backup-') as work:
        snapshot = Path(work) / backup_path.name
        assets = Path(work) / 'assets'
        assets.mkdir()
        with closing(sqlite3.connect(snapshot)) as destination:
            connection.backup(destination)
            destination.execute('PRAGMA journal_mode=DELETE')
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
                    shutil.copy2(file, target)
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
