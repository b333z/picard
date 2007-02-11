# -*- coding: utf-8 -*-
#
# Picard, the next-generation MusicBrainz tagger
# Copyright (C) 2004 Robert Kaye
# Copyright (C) 2006 Lukáš Lalinský
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.

import glob
import os.path
import shutil
from PyQt4 import QtCore
from picard.metadata import Metadata
from picard.ui.item import Item
from picard.util import LockableObject, needs_write_lock, needs_read_lock, encode_filename, decode_filename, format_time
from picard.util.thread import spawn, proxy_to_main

class File(LockableObject, Item):

    __id_counter = 0

    @staticmethod
    def new_id():
        File.__id_counter += 1
        return File.__id_counter

    PENDING = 0
    NORMAL = 1
    CHANGED = 2
    ERROR = 3
    SAVED = 4

    def __init__(self, filename):
        super(File, self).__init__()
        self.id = self.new_id()
        self.filename = filename
        self.base_filename = os.path.basename(filename)
        self.state = File.PENDING
        self.error = None

        self.orig_metadata = Metadata()
        self.user_metadata = Metadata()
        self.server_metadata = Metadata()
        self.saved_metadata = self.server_metadata
        self.metadata = self.user_metadata

        self.orig_metadata['title'] = os.path.basename(self.filename)
        self.orig_metadata['~#length'] = 0
        self.orig_metadata['~length'] = format_time(0)

        self.user_metadata.copy(self.orig_metadata)
        self.server_metadata.copy(self.orig_metadata)

        self.similarity = 1.0
        self.parent = None

    def __str__(self):
        return '<File #%d %r>' % (self.id, self.base_filename)

    def load(self):
        """Load metadata from the file."""
        del self.metadata['title']
        spawn(self._load_thread)

    def _load_thread(self):
        self.log.debug("Loading file %r", self)
        error = None
        try:
            self._load()
        except Exception, e:
            self.log.error(traceback.format_exc())
            error = str(e)
        proxy_to_main(self._load_thread_finished, error)

    def _load_thread_finished(self, error):
        self.error = error
        self.state = (self.error is None) and File.NORMAL or File.ERROR
        self._post_load()
        self.update()
        puid = self.metadata['musicip_puid']
        trackid = self.metadata['musicbrainz_trackid']
        albumid = self.metadata['musicbrainz_albumid']
        if puid and trackid:
            self.tagger.puidmanager.add(puid, trackid)
        if albumid:
            if trackid:
                self.tagger.move_file_to_album(self, albumid)
            else:
                self.tagger.move_file_to_track(self, albumid, trackid)

    def _post_load(self):
        self.metadata['~length'] = format_time(self.metadata['~#length'])
        if not 'title' in self.metadata:
            self.metadata['title'] = os.path.basename(self.filename)
        self.orig_metadata.copy(self.metadata)

    def _load(self):
        """Load metadata from the file."""
        raise NotImplementedError

    def save(self):
        """Save the metadata."""
        raise NotImplementedError

    def save_images(self):
        """Save the cover images to disk."""
        if not "~artwork" in self.metadata:
            return
        filename = self.config.setting["cover_image_filename"]
        if not filename:
            filename = "cover"
        filename = os.path.join(os.path.dirname(self.filename),
                                filename)
        filename = encode_filename(filename)
        images = self.metadata.getall("~artwork")
        i = 0
        for mime, data in images:
            image_filename = filename
            ext = ".jpg" # TODO
            if i > 0:
                image_filename = "%s (%d)" % (filename, i)
            i += 1
            while os.path.exists(image_filename + ext):
                if os.path.getsize(image_filename + ext) == len(data):
                    self.log.debug("Identical file size, not saving %r", image_filename)
                    break
                image_filename = "%s (%d)" % (filename, i)
                i += 1
            else:
                self.log.debug("Saving cover images to %r", image_filename)
                f = open(image_filename + ext, "wb")
                f.write(data)
                f.close()

    def move_additional_files(self, old_filename):
        old_path = encode_filename(os.path.dirname(old_filename))
        new_path = encode_filename(os.path.dirname(self.filename))
        patterns = encode_filename(self.config.setting["move_additional_files_pattern"])
        patterns = filter(bool, [p.strip() for p in patterns.split()])
        files = []
        for pattern in patterns:
            pattern = os.path.join(old_path, pattern)
            for old_file in glob.glob(pattern):
                if self.tagger.get_file_by_filename(decode_filename(old_file)):
                    self.log.debug("File loaded in the tagger, not moving %r", old_file)
                    continue
                new_file = os.path.join(new_path, os.path.basename(old_file))
                self.log.debug("Moving %r to %r", old_file, new_file)
                shutil.move(old_file, new_file)

    def remove(self):
        if self.parent:
            self.log.debug("Removing %s from %s", self, self.parent)
            self.parent.remove_file(self)

    def move(self, parent):
        if parent != self.parent:
            self.log.debug("Moving %s from %s to %s", self, self.parent, parent)
            if self.parent:
                self.parent.remove_file(self)
                self.state = self.CHANGED
            self.parent = parent
            self.parent.add_file(self)
            self.tagger.puidmanager.update(self.metadata['musicip_puid'], self.metadata['musicbrainz_trackid'])

    def update(self, signal=True):
        self.lock_for_read()
        metadata1 = self.orig_metadata
        metadata2 = self.metadata
        self.unlock()
        similarity = metadata1.compare(metadata2)
        self.lock_for_write()
        self.similarity = similarity
        self.unlock()
        if signal:
            self.log.debug(u"Updating file %s", self)
            self.parent.update_file(self)

    def can_save(self):
        """Return if this object can be saved."""
        return True

    def can_remove(self):
        """Return if this object can be removed."""
        return True

    def can_edit_tags(self):
        """Return if this object supports tag editing."""
        return True

    def can_analyze(self):
        """Return if this object can be fingerprinted."""
        return True

    def can_refresh(self):
        return False

    def item_column_text(self, column):
        return Item.item_column_text(self, column)

    def _info(self, file):
        self.metadata["~#length"] = int(file.info.length * 1000)
        if hasattr(file.info, 'bitrate') and file.info.bitrate:
            self.metadata['~#bitrate'] = file.info.bitrate / 1000.0
        if hasattr(file.info, 'sample_rate') and file.info.sample_rate:
            self.metadata['~#sample_rate'] = file.info.sample_rate
        if hasattr(file.info, 'channels') and file.info.channels:
            self.metadata['~#channels'] = file.info.channels
        if hasattr(file.info, 'bits_per_sample') and file.info.bits_per_sample:
            self.metadata['~#bits_per_sample'] = file.info.bits_per_sample
        self.metadata['~format'] = self.__class__.__name__.replace('File', '')

    def get_state(self):
        return self._state

    def set_state(self, state, update=False):
        self._state = state
        if update:
            self.update()
        self.tagger.emit(QtCore.SIGNAL("file_state_changed"))

    state = property(get_state, set_state)
