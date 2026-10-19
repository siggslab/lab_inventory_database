"""Pushing the interface script into a workbook's bound Apps Script project.

Nothing here contacts Google. The shapes worth pinning are the ones Google refuses
without explaining: the file types are exactly ``SERVER_JS | HTML | JSON``, the project
is bound by ``parentId`` rather than by a binding list, and a second install must find
the project it already made instead of creating one nobody knows about.
"""

import json

import pytest

from inventory_mcp import cli, uiscript
from inventory_mcp.auth import ADMIN_SCOPES, AuthError
from inventory_mcp.config import Config

BOOK = "1FakeSpreadsheetIdForTests00000000000000"


class _Call:
    def __init__(self, log, name, kwargs, result):
        self.log, self.name, self.kwargs, self.result = log, name, kwargs, result

    def execute(self):
        self.log.append((self.name, self.kwargs))
        return self.result if isinstance(self.result, dict) else {}


class _FakeDrive:
    """Only the two calls the installer makes, with the appProperties it should see."""

    def __init__(self, log, app_properties=None, create_result=None):
        self.log = log
        self.app_properties = dict(app_properties or {})
        self.create_result = create_result if create_result is not None else {"scriptId": "SCRIPT-1"}

    def files(self):
        return _FakeFiles(self)


class _FakeFiles:
    """Signatures mirror Drive v3, whose client raises TypeError on an unknown keyword.

    That strictness is the reason these are spelled out rather than taken as ``**kw``:
    the installer once passed ``updatePropertyVisibility``, a Drive v2 leftover that v3
    never had, and discovered it locally *after* Google had created the project -- which
    left a bound script with nothing recording which workbook it belonged to.
    """

    def __init__(self, drive):
        self.drive = drive

    def get(self, *, fileId, fields=None, supportsAllDrives=None):  # noqa: A002
        return _Call(self.drive.log, "files.get",
                     {"fileId": fileId, "fields": fields,
                      "supportsAllDrives": supportsAllDrives},
                     {"appProperties": self.drive.app_properties})

    def update(self, *, fileId, body=None, fields=None,  # noqa: A002
               supportsAllDrives=None):
        # Behave like Drive: what was written becomes what the next read returns.
        self.drive.app_properties.update((body or {}).get("appProperties", {}))
        return _Call(self.drive.log, "files.update",
                     {"fileId": fileId, "body": body, "fields": fields,
                      "supportsAllDrives": supportsAllDrives},
                     {"id": fileId})


class _FakeScript:
    def __init__(self, log, create_result=None, fail_with=None):
        self.log = log
        self.create_result = create_result if create_result is not None else {"scriptId": "SCRIPT-1"}
        self.fail_with = fail_with

    def projects(self):
        return _FakeProjects(self)


class _FakeProjects:
    def __init__(self, script):
        self.script = script

    def create(self, **kw):
        if self.script.fail_with:
            raise self.script.fail_with
        return _Call(self.script.log, "projects.create", kw, self.script.create_result)

    def updateContent(self, **kw):  # noqa: N802 - the API's own spelling
        if self.script.fail_with:
            raise self.script.fail_with
        return _Call(self.script.log, "projects.updateContent", kw, {"scriptId": kw["scriptId"]})


@pytest.fixture
def seams(monkeypatch):
    """Swap both services out and hand back the recorder plus the knobs."""
    from inventory_mcp import auth

    state = {"log": [], "drive": {}, "script": {}}

    def wire(*, app_properties=None, create_result=None, script_error=None):
        state["drive"] = _FakeDrive(state["log"], app_properties)
        state["script"] = _FakeScript(state["log"], create_result, script_error)
        monkeypatch.setattr(auth, "drive_service", lambda config, **kw: state["drive"])
        monkeypatch.setattr(auth, "script_service", lambda config, **kw: state["script"])
        return state

    return wire


def _names(log):
    return [entry[0] for entry in log]


# -- the contents we push ----------------------------------------------------


def test_the_script_ships_inside_the_package():
    text = uiscript.source()
    assert len(text) > 1000, "an empty or truncated script would install happily and do nothing"
    for entry_point in ("function onOpen", "function inventoryAddButtons_", "btn_move_item"):
        assert entry_point in text, f"{entry_point} is gone from the shipped script"


def test_the_project_files_use_only_types_the_api_accepts():
    """The enum is SERVER_JS | HTML | JSON. A fourth value fails the whole request,
    and the error names the file, not the field that is wrong."""
    payload = uiscript.files()
    assert [item["type"] for item in payload] == ["SERVER_JS", "JSON"]
    assert [item["name"] for item in payload] == ["InventoryUi", "appsscript"]
    assert all(item["source"].strip() for item in payload)


def test_the_manifest_pins_the_scopes_the_sheet_needs():
    """Inferred scopes mean one stray service call in a future edit widens the consent
    prompt for every editor at once. Pinning turns that into an error at the line."""
    pinned = json.loads(uiscript.files()[1]["source"])
    assert pinned["oauthScopes"] == [
        "https://www.googleapis.com/auth/spreadsheets.currentonly",
    ]
    # Parked, not rejected: declaring it left the sheet-side run holding no scopes at all.
    # See uiscript.manifest(). Asserted so it cannot creep back in unnoticed.
    assert "https://www.googleapis.com/auth/userinfo.email" not in pinned["oauthScopes"]
    assert not [scope for scope in pinned["oauthScopes"]
                if any(word in scope for word in ("drive", "urlfetch", "script"))], \
        "the icon is inline base64 and the script is pushed by the CLI; neither needs a scope"
    assert pinned["runtimeVersion"] == "V8"


# -- creating, then updating -------------------------------------------------


def test_installing_binds_a_new_project_to_this_workbook(seams):
    state = seams()
    done = uiscript.install(Config(backend="drive", drive_folder_id="F"), BOOK)

    created = [kw for name, kw in state["log"] if name == "projects.create"]
    assert created == [{"body": {"title": uiscript.PROJECT_TITLE, "parentId": BOOK}}], (
        "parentId is what binds the project to the sheet; there is no bindings field")
    assert done["created"] is True
    assert done["script_id"] == "SCRIPT-1"
    assert "projects.updateContent" in _names(state["log"])


def test_the_new_project_id_is_recorded_before_the_content_goes_up(seams):
    """An installed-but-unrecorded project is invisible to the next run, which would
    then quietly make a second one."""
    state = seams()
    uiscript.install(Config(backend="drive", drive_folder_id="F"), BOOK)
    order = _names(state["log"])
    assert order.index("projects.create") < order.index("files.update") \
        < order.index("projects.updateContent")
    assert state["drive"].app_properties[uiscript.PROPERTY_KEY] == "SCRIPT-1"


def test_a_project_that_could_not_be_recorded_names_itself(seams, monkeypatch):
    """Recording can fail after creating succeeded -- that is how this was written.

    The project is real and bound by then, so the message has to carry its id: without
    one there is nothing to delete and nothing to point the next run at, and the second
    install silently makes another.
    """
    state = seams()

    def refuse(drive, book, script_id):
        raise RuntimeError("the workbook rejected the write")

    monkeypatch.setattr(uiscript, "_record_project_id", refuse)
    with pytest.raises(uiscript.InstallError) as excinfo:
        uiscript.install(Config(backend="drive", drive_folder_id="F"), BOOK)
    message = str(excinfo.value)
    assert "SCRIPT-1" in message, "the id is the only way back to this project"
    assert "script.google.com/home" in message, "and where to delete it"
    assert "projects.updateContent" not in _names(state["log"]), (
        "no content goes into a project nobody is tracking")


def test_a_second_install_updates_the_project_it_already_made(seams):
    state = seams(app_properties={uiscript.PROPERTY_KEY: "SCRIPT-EXISTING"})
    done = uiscript.install(Config(backend="drive", drive_folder_id="F"), BOOK)

    assert "projects.create" not in _names(state["log"]), (
        "creating a second project leaves one running that nobody knows about")
    updated = [kw for name, kw in state["log"] if name == "projects.updateContent"]
    assert updated[0]["scriptId"] == "SCRIPT-EXISTING"
    assert done["created"] is False


def test_the_whole_project_is_replaced_rather_than_merged(seams):
    """updateContent swaps the file set wholesale. Worth asserting explicitly: it is
    what stops a half-edited script left in the editor from surviving an install."""
    state = seams()
    uiscript.install(Config(backend="drive", drive_folder_id="F"), BOOK)
    sent = [kw for name, kw in state["log"] if name == "projects.updateContent"][0]
    assert {item["name"] for item in sent["body"]["files"]} == {"InventoryUi", "appsscript"}


def test_a_project_google_did_not_name_is_not_counted_as_installed(seams):
    state = seams(create_result={"name": "no id in this answer"})
    with pytest.raises(uiscript.InstallError):
        uiscript.install(Config(backend="drive", drive_folder_id="F"), BOOK)
    assert "projects.updateContent" not in _names(state["log"])
    assert state["drive"].app_properties == {}, "nothing usable was recorded either"


# -- refusing before anything happens ----------------------------------------


def test_no_workbook_stops_it_before_any_call(seams):
    state = seams()
    with pytest.raises(uiscript.InstallError) as excinfo:
        uiscript.install(Config(backend="drive", drive_folder_id="F"), "")
    assert "publish the interface first" in str(excinfo.value)
    assert state["log"] == []


def test_a_token_without_the_setup_scope_is_refused_before_creating(seams, monkeypatch):
    """Creating a project is the irreversible part, so the scope check has to land
    ahead of it -- which it does because building the service asks for those scopes."""
    from inventory_mcp import auth

    state = seams()

    def refuse(config, **kw):
        raise AuthError("the stored token does not cover: " + ADMIN_SCOPES[0])

    monkeypatch.setattr(auth, "script_service", refuse)
    with pytest.raises(AuthError) as excinfo:
        uiscript.install(Config(backend="drive", drive_folder_id="F"), BOOK)
    assert ADMIN_SCOPES[0] in str(excinfo.value)
    assert state["log"] == [], "no project was created with a token that cannot authorise it"


def test_the_packaged_source_beats_an_empty_file(tmp_path, monkeypatch):
    monkeypatch.setattr(uiscript, "SCRIPT_SOURCE", tmp_path / "empty.gs")
    (tmp_path / "empty.gs").write_text("   \n", encoding="utf-8")
    with pytest.raises(uiscript.InstallError, match="empty"):
        uiscript.source()


# -- a cloud project where the API is switched off ----------------------------

#: Google's live answer for a project that never had the Script API enabled. The prose
#: says "permission denied" and blames prior use; the console link sits in the details.
DISABLED_BODY = json.dumps({
    "error": {
        "code": 403,
        "message": ("Apps Script API has not been used in project 13324608815 before "
                    "or it is disabled."),
        "status": "PERMISSION_DENIED",
        "details": [
            {"@type": "type.googleapis.com/google.rpc.ErrorInfo",
             "reason": "SERVICE_DISABLED", "domain": "googleapis.com",
             "metadata": {
                 "serviceTitle": "Apps Script API",
                 "consumer": "projects/13324608815",
                 "service": "script.googleapis.com",
                 "activationUrl": ("https://console.developers.google.com/apis/api/"
                                   "script.googleapis.com/overview?project=13324608815")}},
            {"@type": "type.googleapis.com/google.rpc.Help", "links": [
                {"description": "Google developers console API activation",
                 "url": "https://console.developers.google.com/apis/library"}]},
        ],
    }}).encode()

OTHER_BODY = json.dumps({"error": {"code": 400, "message": "Invalid file name.",
                                   "status": "INVALID_ARGUMENT"}}).encode()


class _Http(Exception):
    """The shape of ``googleapiclient.errors.HttpError`` that we actually read."""

    def __init__(self, body):
        super().__init__(body.decode("utf-8", "replace"))
        self.content = body


def test_a_disabled_api_becomes_the_console_link(seams):
    """Otherwise setup ends in a traceback whose prose points at permissions."""
    state = seams(script_error=_Http(DISABLED_BODY))
    with pytest.raises(uiscript.InstallError) as excinfo:
        uiscript.install(Config(backend="drive", drive_folder_id="F"), BOOK)
    message = str(excinfo.value)
    assert "not enabled" in message
    assert "script.googleapis.com/overview?project=13324608815" in message, (
        "the link Google itself supplied is the whole remedy")
    assert "projects.create" not in _names(state["log"])


def test_any_other_google_error_keeps_its_own_answer(seams):
    seams(script_error=_Http(OTHER_BODY))
    with pytest.raises(_Http) as excinfo:
        uiscript.install(Config(backend="drive", drive_folder_id="F"), BOOK)
    assert b"Invalid file name" in excinfo.value.content, (
        "translating this one would hide what Google actually said")


@pytest.mark.parametrize("body", [b"not json", b"{}", b'{"error": {}}',
                                  b'{"error": {"details": "no list here"}}'])
def test_no_remedy_is_invented_when_the_details_say_nothing(body):
    assert uiscript._disabled_api_remedy(_Http(body)) == ""
    assert uiscript._disabled_api_remedy(RuntimeError("no content attribute")) == ""


def test_the_real_http_error_carries_its_body_as_bytes():
    """Pinned because the translation above reads ``.content`` and nothing else."""
    pytest.importorskip("googleapiclient.errors", reason="needs the 'drive' extra")
    from googleapiclient.errors import HttpError

    response = type("Resp", (), {"status": 403, "reason": "Forbidden"})()
    error = HttpError(response, DISABLED_BODY, uri="https://script.googleapis.com")
    assert isinstance(error.content, bytes)
    assert "not enabled" in uiscript._disabled_api_remedy(error)


# -- the command reaching the installer ---------------------------------------


class _Store:
    """The slice of ``DriveWorkbookStore`` that ``ui --install`` touches.

    ``workbook_id`` is a method here because it is one there. The first version of the
    command passed the method instead of calling it, and Drive answered with a 404
    quoting ``<bound method DriveWorkbookStore.workbook_id ...>`` as the file name.
    """

    def __init__(self, book):
        self.config = Config(backend="drive", drive_folder_id="F")
        self._book = book

    def workbook_id(self):
        return self._book


def test_the_command_hands_the_installer_an_id_not_an_accessor(seams):
    state = seams()
    assert cli._install_ui_script(_Store(BOOK)) == 0
    created = [kw for name, kw in state["log"] if name == "projects.create"]
    assert created == [{"body": {"title": uiscript.PROJECT_TITLE, "parentId": BOOK}}]


def test_a_workbook_that_is_not_there_stops_before_the_script_api(seams):
    state = seams()
    assert cli._install_ui_script(_Store(None)) == 1
    assert state["log"] == []


def test_an_uncalled_accessor_is_refused_locally(seams):
    """Cheap enough to catch here rather than in a Google error about a bad file id."""
    state = seams()
    store = _Store(BOOK)
    with pytest.raises(uiscript.InstallError) as excinfo:
        uiscript.install(store.config, store.workbook_id)
    assert "not an id" in str(excinfo.value)
    assert state["log"] == []


# -- the command surface ------------------------------------------------------


def test_ui_advertises_the_install_and_its_gate():
    parsed = cli._build_parser().parse_args(["ui", "--install", "--admin"])
    assert parsed.install and parsed.admin
    assert parsed.command == "ui", "the refusal message names the subcommand from here"


def test_install_is_not_silently_ignored_by_an_ordinary_ui_run():
    """Without --install a plain `ui` must keep doing exactly what it did before."""
    parsed = cli._build_parser().parse_args(["ui"])
    assert getattr(parsed, "install", False) is False
    assert getattr(parsed, "admin", False) is False


def test_reset_is_gated_because_it_deletes_a_tab():
    parsed = cli._build_parser().parse_args(["ui", "--reset"])
    assert parsed.reset and not parsed.admin
    assert cli._require_admin(parsed, "--reset",
                              effect="deleting the interface tabs") == 2, \
        "rebuilding from nothing deletes whatever a person left there"


def test_reset_with_admin_is_allowed_and_narrowable():
    parsed = cli._build_parser().parse_args(["ui", "--reset", "--problems", "--admin"])
    assert cli._require_admin(parsed, "--reset") == 0
    assert parsed.problems and not parsed.layout, \
        "reset follows whichever half of the run was asked for"
