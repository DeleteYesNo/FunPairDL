"""Google Drive: shared files and folders (synthetic ids and names)."""
from funpairdl.core.queue_manager import QueueManager
from funpairdl.providers import gdrive as gd
from funpairdl.providers.registry import ProviderRegistry
from funpairdl.providers.ytdlp_generic import YtdlpGenericProvider
from funpairdl.utils.url_parser import detect_provider

FOLDER = "https://drive.google.com/drive/folders/1MockFolderId0000000000000?usp=drive_link"
FILE = "https://drive.google.com/file/d/1MockFileId00000000000000000/view?usp=sharing"

LISTING = """<div class="flip-entries">
<div class="flip-entry" id="entry-1MockFileId00000000000000000" tabindex="0" role="link"><div class="flip-entry-info">
<a href="https://drive.google.com/file/d/1MockFileId00000000000000000/view?usp=drive_web" target="_blank">
<div class="flip-entry-title">Sample Work &amp; Co.mp4</div></a></div></div>
<div class="flip-entry" id="entry-1MockSubFolder000000000000" tabindex="0" role="link"><div class="flip-entry-info">
<a href="https://drive.google.com/drive/folders/1MockSubFolder000000000000" target="_blank">
<div class="flip-entry-title">Extras</div></a></div></div>
</div>"""


def test_ids_from_links():
    assert gd.folder_id(FOLDER) == "1MockFolderId0000000000000"
    assert gd.file_id(FILE) == "1MockFileId00000000000000000"
    assert gd.file_id("https://drive.google.com/open?id=1MockFileId00000000000000000") == "1MockFileId00000000000000000"
    assert gd.file_id("https://drive.google.com/uc?id=1MockFileId00000000000000000&export=download") \
        == "1MockFileId00000000000000000"
    assert gd.file_id(FOLDER) == "" and gd.folder_id(FILE) == ""
    assert gd.file_id("https://example.com/file/d/1MockFileId00000000000000000/view") == ""


def test_parse_folder_lists_files_and_subfolders():
    entries = gd.parse_folder(LISTING)
    assert entries == [
        {"id": "1MockFileId00000000000000000", "name": "Sample Work & Co.mp4", "folder": False},
        {"id": "1MockSubFolder000000000000", "name": "Extras", "folder": True},
    ]


def test_disposition_filename():
    assert gd.name_from_disposition('attachment; filename="Sample Work.mp4"') == "Sample Work.mp4"
    assert gd.name_from_disposition("attachment; filename*=UTF-8''%E6%A8%A3%E6%9C%AC.mp4") == "樣本.mp4"


def test_routing_file_to_provider_folder_to_bundle():
    assert detect_provider(FILE) == "gdrive" and detect_provider(FOLDER) == "gdrive"
    assert ProviderRegistry().get_provider(FILE).name == "gdrive"
    assert not YtdlpGenericProvider.can_handle(FILE)
    assert QueueManager._is_bundle_url(FOLDER) and not QueueManager._is_bundle_url(FILE)
