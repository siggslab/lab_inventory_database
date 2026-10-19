/**
 * Lab inventory -- the workbook's own buttons.
 *
 * INSTALL (about a minute, no tooling):
 *   1. Open the workbook, then Extensions > Apps Script.
 *   2. Delete whatever is in Code.gs, paste this whole file in, save.
 *   3. Reload the spreadsheet. A "Lab inventory" menu appears.
 *
 * WHAT THIS IS. Every action on Start can also be done by hand in the data tabs, and
 * nothing here is needed for the database to work. A button exists because typing an
 * id into four places is how typos get into a lab record. So each action tries to
 * PROVE the edit is legal -- from the vocabulary published on `options` and the rows
 * already in the workbook -- and refuses on the spot when it cannot prove it, naming
 * the reason and what would satisfy it. Nothing is queued, staged, marked pending or
 * waiting for a sync: a refused press changed nothing, and a successful one is saved.
 *
 * WHY IT READS `options` INSTEAD OF KNOWING THINGS. The enums, required flags,
 * foreign-key targets, id prefixes, legal units and the coordinates of every input box
 * are generated from the schema by `inventory-mcp ui` and published on the hidden
 * `options` tab. This file deliberately contains no copy of any of that. A second
 * hand-written list of valid statuses is a second rulebook, and the second rulebook is
 * the one that quietly disagrees with the server while everyone keeps trusting it.
 *
 * WHAT IT CANNOT DO. Two things, both worth saying out loud:
 *   - It cannot detect a concurrent edit. LockService serialises presses of this script
 *     against each other, not against someone typing in a tab at the same moment. The
 *     last writer wins, exactly as it does with two people editing the sheet by hand.
 *   - It works from the problem report on `database_errors`, which is only as fresh as
 *     the last time `inventory-mcp ui` (or init) wrote it. An error introduced by hand
 *     since then is invisible here.
 */

/** The three derived tabs, named in one place. */
var INVENTORY_OPTIONS_TAB = 'options';
var INVENTORY_START_TAB = 'Start';
var INVENTORY_ERRORS_TAB = 'database_errors';

/**
 * The layout this file reads `options` with. Bumped together with LAYOUT_VERSION in
 * inventory_mcp/sheetui.py; a workbook laid out by another release gets refused rather
 * than misread, because every coordinate below depends on the blocks being where this
 * version put them.
 */
var INVENTORY_LAYOUT_VERSION = 2;

/** Block anatomy on `options`: title in row 1, headers in row 2, values from row 3. */
var INVENTORY_TITLE_ROW = 1;
var INVENTORY_HEADER_ROW = 2;

/** Data tabs carry their header in row 1, so their records start at row 2. */
var INVENTORY_DATA_FIRST_ROW = 2;

/** Rows added at a time when a tab runs out of room, so growth costs one call rarely. */
var INVENTORY_ROW_BLOCK = 25;


/* =========================================================================
 * Entry points
 * ======================================================================= */

/**
 * Builds the menu from the published action list rather than a list typed here, so a
 * form added in Python shows up without anyone remembering to edit this file too.
 *
 * This runs as a simple trigger, which means AuthMode.LIMITED: reading this workbook is
 * allowed, anything needing authorization is not, and there are thirty seconds. Hence no
 * script properties and nothing that can fail quietly -- if the interface will not load,
 * the menu still appears and offers the reason.
 */
function onOpen() {
  var ui = SpreadsheetApp.getUi();
  var ctx = null;
  try {
    ctx = loadInterface_();
  } catch (problem) {
    // The reason is worked out when someone asks for it, not stored here. Storing it
    // would need script properties, which is precisely what this mode forbids -- and a
    // throw inside this catch would leave no menu at all, looking exactly like nobody
    // having installed the script.
    ui.createMenu('Lab inventory (broken)')
      .addItem('Why is the interface broken?', 'inventoryExplainFailure_')
      .addToUi();
    return;
  }
  var menu = ui.createMenu('Lab inventory');
  var keys = Object.keys(ctx.actions);
  for (var i = 0; i < keys.length; i++) {
    // A menu item names a global function; it does not hold one. Apps Script serialises the
    // name and looks it up when the item is clicked, so there is nothing to close over --
    // which is the same reason the image buttons need one global per action anyway.
    menu.addItem(menuTitle_(ctx, keys[i]), 'btn_' + keys[i]);
  }
  menu.addSeparator();
  menu.addItem('Add press buttons to Start', 'inventoryAddButtons_');
  menu.addItem('Say why the interface is wrong', 'inventoryExplainFailure_');
  menu.addToUi();
}

/** Without this the menu only appears after a reload, which reads as a failed install. */
function onInstall() {
  onOpen();
}

/** One global function per action, because an image button can only name a function. */
function btn_add_sample() { runAction_('add_sample'); }
function btn_add_supply() { runAction_('add_supply'); }
function btn_add_container() { runAction_('add_container'); }
function btn_record_measurement() { runAction_('record_measurement'); }
function btn_move_item() { runAction_('move_item'); }
function btn_set_status() { runAction_('set_status'); }
function btn_rename_item() { runAction_('rename_item'); }
function btn_edit_notes() { runAction_('edit_notes'); }
function btn_retire_item() { runAction_('retire_item'); }

/**
 * Says what is wrong, worked out now rather than remembered.
 *
 * An error left over from a layout that has since been fixed would be a worse lie than
 * no message, so the interface is loaded again here -- in FULL mode, where this can say
 * more than it could during onOpen.
 */
function inventoryExplainFailure_() {
  var ui = SpreadsheetApp.getUi();
  var stored = properties_().getProperty('inventory_interface_error');
  var message;
  try {
    loadInterface_();
    message = stored
      ? 'The interface loads fine now. The last thing that went wrong unexpectedly was:\n\n' +
        stored
      : 'The interface loads fine and nothing has gone wrong recently.';
  } catch (problem) {
    message = String(problem && problem.message || problem);
  }
  ui.alert('Lab inventory', message, ui.ButtonSet.OK);
}

function menuTitle_(ctx, key) {
  var action = ctx.actions[key];
  return action.title + (action.canDoIt ? '' : ' (refuses)');
}


/* =========================================================================
 * Press buttons on Start
 * ======================================================================= */

/**
 * A play triangle on a blue bar, 96x26, carried as bytes inside this file.
 *
 * Inline rather than fetched because everything else about this install is a paste: a URL
 * would need UrlFetchApp and its extra consent scope for every editor, and a Drive file
 * would need the drive scope plus sharing with each of them. Two hundred bytes costs none
 * of that. It is a shape rather than a word because there is no way to draw text here --
 * the block's own label sits beside it, and the hover text spells it out in full.
 */
var INVENTORY_BUTTON_PNG =
  'iVBORw0KGgoAAAANSUhEUgAAAGAAAAAaCAYAAABIIVmfAAAAi0lEQVR42u3auw2AMBCD4VuBlr2YJH' +
  'OxF1uEii5IFAhj5z/JPfIn8bhQdTNrOzp5L/VkKEqIQTFCBAoRI1AGAPMC/P0ir4lFcAFIhbADSMOw' +
  'BkjAiAFwhYgDcMOIBnCAAAAAbkE8hAHgNZQPMQBYRbCMYyPKeQABYNJz4WXbO/kuwz8jKEZYPhia0k' +
  '9urspWzWBk/wAAAABJRU5ErkJggg==';

/**
 * Alt text is how a button is recognised next time. The API gives an over-grid image no
 * id and no way to list by name, so this tag is the only handle there is.
 */
var INVENTORY_BUTTON_TAG = 'lab-inventory-button:';

var INVENTORY_BUTTON_WIDTH = 88;
var INVENTORY_BUTTON_HEIGHT = 22;

/**
 * Puts one button per usable action on Start, at the anchor cells the generator published.
 *
 * Deliberately not automatic: this puts objects on someone else's sheet, and an image that
 * stays invisible until the tab is reloaded would look like nothing had happened. So it is
 * asked for, and it reports what it did. Actions the script will refuse get no button -- a
 * rectangle that only says no is worse than the menu item that says no with a reason.
 */
function inventoryAddButtons_() {
  var ui = SpreadsheetApp.getUi();
  var ctx;
  try {
    ctx = loadInterface_();
  } catch (problem) {
    ui.alert('Lab inventory', 'No buttons were added. ' +
      String(problem && problem.message || problem), ui.ButtonSet.OK);
    return;
  }

  var mine = {};
  var images = ctx.start.getImages();
  for (var i = 0; i < images.length; i++) {
    var tag = String(images[i].getAltTextTitle() || '');
    if (tag.indexOf(INVENTORY_BUTTON_TAG) === 0) mine[tag] = images[i];
  }

  var keys = Object.keys(ctx.actions);
  var added = [], moved = [], skipped = [], failed = [];
  for (i = 0; i < keys.length; i++) {
    var action = ctx.actions[keys[i]];
    if (!action.canDoIt) {
      skipped.push(action.title);
      continue;
    }
    var where = parseA1_(action.anchor);
    if (!where) {
      skipped.push(action.title + ' (this build published no anchor cell for it)');
      continue;
    }
    var tag = INVENTORY_BUTTON_TAG + keys[i];
    if (mine[tag]) {
      // Re-run after the layout moved: follow the anchor instead of adding a twin.
      try {
        mine[tag].setAnchorCell(ctx.start.getRange(where.row, where.col));
        moved.push(action.title);
      } catch (problem) {
        failed.push(action.title + ' -- ' + String(problem && problem.message || problem));
      }
      continue;
    }
    // One button failing has to cost exactly one button. Putting these calls outside a
    // catch meant the fourth failure ended the loop, so six actions silently got no
    // button and the summary said nothing about them -- which is how a partial install
    // gets mistaken for a finished one.
    try {
      // Column before row here, unlike getRange(row, column). Swapping them is silent.
      var image = ctx.start.insertImage(buttonBlob_(action.title), where.col, where.row);
      image.setAltTextTitle(tag);
      image.setAltTextDescription(action.title);
      image.setWidth(INVENTORY_BUTTON_WIDTH);
      image.setHeight(INVENTORY_BUTTON_HEIGHT);
      image.assignScript('btn_' + keys[i]);
      added.push(action.title);
    } catch (problem) {
      failed.push(action.title + ' -- ' + String(problem && problem.message || problem));
    }
  }

  var lines = [];
  if (added.length) lines.push('Added ' + added.length + ' button' + (added.length === 1 ? '' : 's') + '.');
  if (moved.length) lines.push(moved.length + ' already there, moved to the current cells.');
  if (skipped.length) {
    lines.push('No button for: ' + skipped.join(', ') +
      '. Those need something a script cannot prove; the menu explains why.');
  }
  if (failed.length) {
    lines.push('Could not add: ' + failed.join('; ') +
      '. Whatever failed is a Sheets problem, not a refusal -- the others were added.');
  }
  if (!lines.length) lines.push('Nothing to put on Start.');
  lines.push('If a button does not appear, close and reopen the sheet -- newly inserted ' +
    'images are known to stay invisible until then.');
  lines.push('To remove one: click it and press Delete.');
  ui.alert('Lab inventory', lines.join('\n'), ui.ButtonSet.OK);
}

function buttonBlob_(name) {
  return Utilities.newBlob(Utilities.base64Decode(INVENTORY_BUTTON_PNG), 'image/png', name);
}


/* =========================================================================
 * The published interface, read once per press
 * ======================================================================= */

/**
 * Read `options` and turn it into lookup tables. Throws with a readable message for
 * anything that would otherwise show up as a wrong write later: a missing tab, a layout
 * this file does not understand, an input whose cell is not on Start.
 */
function loadInterface_() {
  var spreadsheet = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = spreadsheet.getSheetByName(INVENTORY_OPTIONS_TAB);
  if (!sheet) {
    throw failure('There is no ' + INVENTORY_OPTIONS_TAB + ' tab in this workbook. Run ' +
      '`inventory-mcp ui` to build the interface, then reload this page.');
  }

  var grid = sheet.getDataRange().getValues();
  var ranges = readBlockIndex_(grid);
  var block = function (name) { return readBlock_(grid, ranges, name); };

  // `stamp` is which build laid the tabs out; `constants` is the vocabulary the checks
  // are written against. Two blocks, and only the first one answers the version question.
  var stamp = pairs_(block('stamp'));
  var constants = pairs_(block('constants'));
  var layoutVersion = stamp['layout_version'];
  if (layoutVersion !== String(INVENTORY_LAYOUT_VERSION)) {
    throw failure('This workbook was laid out by version ' + (layoutVersion || '?') +
      ' of the interface and this script understands version ' + INVENTORY_LAYOUT_VERSION +
      '. Run `inventory-mcp ui` to bring the tabs up to date.');
  }

  var ctx = {
    spreadsheet: spreadsheet,
    start: spreadsheet.getSheetByName(INVENTORY_START_TAB),
    stamp: stamp,
    constants: constants,
    enums: {},
    supplyClasses: column_(block('supply_classes')),
    prefixes: {},
    classes: {},
    foreignKeys: {},
    fields: {},
    assays: {},
    indirections: {},
    actions: {},
    inputs: {},
    problems: []          // findings published on database_errors, kept for the gate
  };

  var names = Object.keys(ranges);
  for (var i = 0; i < names.length; i++) {
    if (names[i].indexOf('enum:') === 0) {
      ctx.enums[names[i].slice(5)] = column_(readBlock_(grid, ranges, names[i]));
    }
  }

  var rows = block('id_prefixes');
  for (i = 0; i < rows.length; i++) {
    var entry = {
      what: rows[i][0], prefix: rows[i][1], table: rows[i][2], extension: rows[i][3],
      nameColumn: rows[i][4], notesColumn: rows[i][5]
    };
    ctx.prefixes[rows[i][1]] = entry;
    // "class reagent" is how the map names a class, and an item's class is what decides
    // which extension row has to exist beside it.
    if (entry.what.indexOf('class ') === 0) {
      ctx.classes[entry.what.slice(6)] = entry;
    }
  }

  rows = block('foreign_keys');
  for (i = 0; i < rows.length; i++) {
    ctx.foreignKeys[rows[i][0] + '.' + rows[i][1]] = rows[i][2];
  }

  rows = block('fields');
  for (i = 0; i < rows.length; i++) {
    ctx.fields[rows[i][0] + '.' + rows[i][1]] = { kind: rows[i][2], required: rows[i][3] === 'yes' };
  }

  rows = block('indirections');
  for (i = 0; i < rows.length; i++) {
    ctx.indirections[rows[i][0]] = rows[i][1];
  }

  rows = block('assays');
  for (i = 0; i < rows.length; i++) {
    var code = text_(rows[i][0]).toLowerCase();
    if (!code || ctx.assays[code]) continue;   // a duplicate code resolves to the first row
    ctx.assays[code] = {
      assayId: text_(rows[i][1]),
      name: text_(rows[i][2]),
      quantity: text_(rows[i][3]),
      canonicalUnit: text_(rows[i][4]),
      resultType: text_(rows[i][5]),
      units: splitList_(rows[i][6])
    };
  }

  rows = block('actions');
  for (i = 0; i < rows.length; i++) {
    ctx.actions[rows[i][0]] = {
      key: rows[i][0],
      title: text_(rows[i][1]),
      writes: text_(rows[i][2]),
      anchor: text_(rows[i][3]),
      result: text_(rows[i][4]),
      canDoIt: text_(rows[i][5]) === 'yes'
    };
  }

  rows = block('inputs');
  for (i = 0; i < rows.length; i++) {
    var action = rows[i][0];
    if (!ctx.actions[action]) {
      throw failure('options lists an input for "' + action + '", which is not an action.');
    }
    if (!ctx.inputs[action]) ctx.inputs[action] = [];
    ctx.inputs[action].push({
      action: action,
      label: text_(rows[i][1]),
      target: text_(rows[i][2]),
      kind: text_(rows[i][3]),
      required: text_(rows[i][4]) === 'yes',
      cell: text_(rows[i][5]),
      hint: text_(rows[i][6])
    });
  }

  if (!ctx.start) {
    throw failure('There is no ' + INVENTORY_START_TAB + ' tab, so there are no boxes to read.');
  }
  readPublishedProblems_(ctx);
  return ctx;
}

/**
 * Find every block's rectangle. The `blocks` index on options says where each one is,
 * which is why this file never has to guess how wide a block is; only the index itself
 * has to be located by name, and it is the last one written.
 */
function readBlockIndex_(grid) {
  var title = rowAt_(grid, INVENTORY_TITLE_ROW);
  var startCol = indexOf_(title, 'blocks');
  if (startCol < 0) {
    throw failure('The ' + INVENTORY_OPTIONS_TAB + ' tab has no `blocks` index, so this ' +
      'script cannot tell where anything is. Run `inventory-mcp ui` to rebuild it.');
  }
  var ranges = {};
  for (var line = INVENTORY_HEADER_ROW + 1; line <= grid.length; line++) {
    var cells = rowAt_(grid, line);
    var name = text_(cells[startCol]);
    if (!name) continue;
    ranges[name] = {
      firstCol: Number(cells[startCol + 1]),
      lastCol: Number(cells[startCol + 2]),
      firstRow: Number(cells[startCol + 3]),
      lastRow: Number(cells[startCol + 4])
    };
  }
  if (!ranges['actions'] || !ranges['inputs']) {
    throw failure('The ' + INVENTORY_OPTIONS_TAB + ' tab predates the form contract this ' +
      'script needs. Run `inventory-mcp ui` to rebuild it.');
  }
  return ranges;
}

/**
 * The value rows of one block, as an array padded to the block's own width.
 *
 * The index publishes sheet columns, so a block starting in column A is listed as 1 --
 * the same way a cell reference spells it, which is what makes the tab readable to a
 * person. `rowAt_` hands back a plain array, so the column comes down by one here.
 */
function readBlock_(grid, ranges, name) {
  var spot = ranges[name];
  if (!spot) {
    throw failure('Nothing on ' + INVENTORY_OPTIONS_TAB + ' is called "' + name + '".');
  }
  var out = [];
  for (var line = spot.firstRow; line <= spot.lastRow; line++) {
    var cells = rowAt_(grid, line);
    var row = [];
    for (var col = spot.firstCol; col <= spot.lastCol; col++) {
      row.push(text_(cells[col - 1]));
    }
    if (row.join('').length) out.push(row);
  }
  return out;
}


/* =========================================================================
 * Running one action
 * ======================================================================= */

function runAction_(key) {
  var lock = LockService.getScriptLock();
  var got = false;
  try {
    got = lock.tryLock(15000);
  } catch (ignored) {
    got = true;   // No lock service means no protection either way; do not fail on it.
  }
  if (!got) {
    announce_(null, key, 'Another press is still going. Wait for it to finish and press again.');
    return;
  }
  var ctx = null;
  try {
    ctx = loadInterface_();
    // Forgetting the last unexpected failure is bookkeeping, not part of the press. It
    // sits here because a stale memo would otherwise be reported after a good write --
    // but `deleteKey` threw on every single press until it was renamed, which is how a
    // diagnostic note once stopped the whole interface from working.
    try {
      properties_().deleteProperty('inventory_interface_error');
    } catch (ignored) {
    }
    var action = ctx.actions[key];
    if (!action) {
      throw refusal('This workbook does not have a "' + key + '" action. Run ' +
        '`inventory-mcp ui` to rebuild the interface.');
    }
    if (!action.canDoIt) {
      throw refusal('No button for this one. It needs something a script cannot prove, so ' +
        'do it by hand in the data tabs -- the columns it touches are in the line above.');
    }
    var handler = INVENTORY_HANDLERS_[key];
    if (!handler) {
      throw refusal('This script does not implement "' + key + '". Do it by hand in the data ' +
        'tabs, or update this workbook with: inventory-mcp ui --install --admin');
    }
    prepare_(ctx, key);
    var made = handler(ctx);
    clearInputs_(ctx);
    announce_(ctx, key, made);
  } catch (problem) {
    var message = (problem && problem.isRefusal)
      ? problem.message
      : 'Something went wrong that this script did not expect, so nothing was written: ' +
        String(problem && problem.message || problem);
    if (!(problem && problem.isRefusal)) {
      // Remembering the last unexpected failure is a courtesy to the next diagnostic,
      // not part of the answer being given now. It must never replace the message the
      // person is owed: this line throwing is how a refused value turned into a
      // "setString is not a function" traceback with no mention of the bad input.
      try {
        properties_().setProperty('inventory_interface_error', message);
      } catch (ignored) {
      }
    }
    announce_(ctx, key, message);
  } finally {
    // Pending cell changes are committed while the lock is still held. Released first,
    // a second press can read the spreadsheet as it was before this one wrote it.
    try {
      SpreadsheetApp.flush();
    } catch (ignored) {
    }
    if (got) lock.releaseLock();
  }
}

/**
 * Read the boxes, resolve who is pressing, and check the standing problem report.
 *
 * The report is the closest thing available to the server's own gate: while an error
 * stands, the server refuses writes to the tables it names, so writing them from here
 * would only deepen the hole. It is consulted where it can be trusted -- a finding that
 * names a table this action is about to write -- and ignored elsewhere, because it is
 * only as fresh as the last `inventory-mcp ui`.
 */
function prepare_(ctx, key) {
  ctx.grid = ctx.start.getDataRange().getValues();
  ctx.actor = actor_(ctx);
  ctx.values = {};
  ctx.inputs = ctx.inputs[key] || [];
  for (var i = 0; i < ctx.inputs.length; i++) {
    var input = ctx.inputs[i];
    var where = parseA1_(input.cell);
    input.row = where.row;
    var value = cellAt_(ctx.grid, where.row, where.col);
    // Both keys: the target is what gets written, the label is what the refusal calls
    // it back to the person who typed it.
    ctx.values[input.target] = value;
    ctx.values[input.label] = value;
  }
}

/** Whose name goes in updated_by: the box, then the signed-in account, then 'unknown'. */
function actor_(ctx) {
  var labels = ctx.grid;
  for (var i = 0; i < labels.length; i++) {
    if (cellAt_(labels, i + 1, 1) === 'Who are you?') {
      var asked = cellAt_(labels, i + 1, 2);
      if (asked) return asked;
      break;
    }
  }
  try {
    var email = Session.getActiveUser().getEmail();
    if (email) return email;
  } catch (ignored) {
    // Consumer accounts across domains return nothing. Fall through to the last resort.
  }
  return 'unknown';
}

/**
 * The problem report, read from the tab rather than recomputed -- the rules live in
 * Python and a JavaScript reimplementation of them is exactly the second rulebook this
 * file avoids. Findings are advisory: they block a write only when one names a table the
 * write is about to touch.
 */
function readPublishedProblems_(ctx) {
  ctx.checkedAt = '';
  ctx.errors = [];
  var sheet = ctx.spreadsheet.getSheetByName(INVENTORY_ERRORS_TAB);
  if (!sheet) return;
  var grid = sheet.getDataRange().getValues();
  var headerRow = -1;
  for (var line = 1; line <= grid.length; line++) {
    if (cellAt_(grid, line, 1) === 'severity') {
      headerRow = line;
      break;
    }
    var checked = cellAt_(grid, line, 1);
    if (checked.indexOf('Checked at ') === 0) {
      ctx.checkedAt = checked.slice('Checked at '.length).replace(/\.$/, '');
    }
  }
  if (headerRow < 0) return;
  for (line = headerRow + 1; line <= grid.length; line++) {
    if (cellAt_(grid, line, 1) !== 'error') continue;
    ctx.errors.push({
      table: cellAt_(grid, line, 2),
      rowId: cellAt_(grid, line, 3),
      column: cellAt_(grid, line, 4),
      code: cellAt_(grid, line, 5),
      message: cellAt_(grid, line, 6)
    });
  }
}

/** A refusal if the published report already calls one of these tables broken. */
function standingErrorFor_(ctx, tables) {
  if (!ctx.errors.length || !tables.length) return '';
  var wanted = {};
  for (var i = 0; i < tables.length; i++) wanted[tables[i]] = true;
  for (i = 0; i < ctx.errors.length; i++) {
    if (!wanted[ctx.errors[i].table]) continue;
    return 'The database already has an error recorded in ' + ctx.errors[i].table +
      ' (' + ctx.errors[i].message + ')' +
      (ctx.checkedAt ? ', found ' + ctx.checkedAt : '') +
      '. Nothing was written. Clear it first -- see the ' + INVENTORY_ERRORS_TAB +
      ' tab, or run `inventory-mcp ui` if that finding has already been fixed.';
  }
  return '';
}


/* =========================================================================
 * Reading and writing tabs
 * ======================================================================= */

/**
 * One tab, read once per press and cached on the context.
 *
 * `rowOf` maps a primary key to its sheet row using column A, which is the primary key
 * of every table -- the store writes each tab's header straight out of the schema, where
 * the key is always the first field. A duplicate id is kept as the first occurrence and
 * noted, because deciding which of two identical ids to edit is not this script's call.
 */
function table_(ctx, title) {
  if (!ctx.tables) ctx.tables = {};
  if (ctx.tables[title]) return ctx.tables[title];

  var sheet = ctx.spreadsheet.getSheetByName(title);
  if (!sheet) {
    throw refusal('There is no "' + title + '" tab in this workbook, so the database here ' +
      'is not the one this interface was built for.');
  }
  var grid = sheet.getDataRange().getValues();
  var header = [];
  var columns = {};
  if (grid.length) {
    for (var col = 0; col < grid[0].length; col++) {
      var name = text_(grid[0][col]);
      header.push(name);
      if (name) columns[name] = col;
    }
  }
  var lastDataRow = INVENTORY_DATA_FIRST_ROW - 1;
  var rowOf = {};
  for (var line = INVENTORY_DATA_FIRST_ROW; line <= grid.length; line++) {
    var cells = grid[line - 1] || [];
    var filled = false;
    for (col = 0; col < cells.length; col++) {
      if (text_(cells[col])) { filled = true; break; }
    }
    if (!filled) continue;
    lastDataRow = line;
    var key = text_(cells[0]);
    if (key && !rowOf.hasOwnProperty(key)) rowOf[key] = line;
  }
  ctx.tables[title] = {
    title: title, sheet: sheet, grid: grid, header: header, columns: columns,
    lastDataRow: lastDataRow, rowOf: rowOf, duplicates: {}
  };
  return ctx.tables[title];
}

/** Every record on a tab as {column: text}, in sheet order, skipping blank rows. */
function records_(ctx, title) {
  var spec = table_(ctx, title);
  var out = [];
  for (var line = INVENTORY_DATA_FIRST_ROW; line <= spec.lastDataRow; line++) {
    var values = {};
    var filled = false;
    for (var col = 0; col < spec.header.length; col++) {
      var value = cellAt_(spec.grid, line, col + 1);
      if (value) filled = true;
      if (spec.header[col]) values[spec.header[col]] = value;
    }
    if (filled) out.push(values);
  }
  return out;
}

/**
 * Refuse if the published report already calls this tab broken.
 *
 * Called at the moment of writing rather than up front, because an action often touches
 * more tabs than its boxes name -- adding a supply writes items, one extension tab and
 * sometimes a reading. Every write passes through here, so none of them can slip past.
 */
function gateOn_(ctx, title) {
  var blocker = standingErrorFor_(ctx, [title]);
  if (blocker) throw refusal(blocker);
}

/**
 * Write one whole record at the bottom of a tab and say which row it landed on.
 *
 * The cells are forced to plain text first. Apps Script otherwise infers types, and a
 * date typed as 2026-10-19 becomes a serial number under someone else's locale -- which
 * is the one thing this project cannot allow, because every other writer here (the
 * server) stores text and reads it back with a fixed format.
 */
function appendRecord_(ctx, title, values) {
  gateOn_(ctx, title);
  var spec = table_(ctx, title);
  var row = nextRow_(ctx, spec);
  writeRecord_(ctx, spec, row, stamped_(ctx, title, values));
  var key = text_(values[spec.header[0]]);
  if (key) spec.rowOf[key] = row;
  return row;
}

/** Change particular columns of an existing row, leaving every other cell alone. */
function patchRecord_(ctx, title, row, values) {
  gateOn_(ctx, title);
  var spec = table_(ctx, title);
  var sheet = spec.sheet;
  values = stamped_(ctx, title, values);
  var names = Object.keys(values);
  if (!names.length) return;
  var runs = contiguousRuns_(spec, names);
  for (var i = 0; i < runs.length; i++) {
    var run = runs[i];
    var line = [];
    for (var col = run.firstCol; col <= run.lastCol; col++) {
      line.push(text_(values[spec.header[col - 1]]));
    }
    var range = sheet.getRange(row, run.firstCol, 1, line.length);
    range.setNumberFormat('@');
    range.setValues([line]);
  }
  for (i = 0; i < names.length; i++) {
    var col2 = spec.columns[names[i]];
    if (col2 !== undefined) setCell_(spec.grid, row, col2 + 1, text_(values[names[i]]));
  }
}

/** Append-shaped version of the same write, for a row that does not exist yet. */
function writeRecord_(ctx, spec, row, values) {
  var line = [];
  for (var col = 0; col < spec.header.length; col++) {
    line.push(text_(values[spec.header[col]]));
  }
  ensureRoom_(spec.sheet, row);
  var range = spec.sheet.getRange(row, 1, 1, line.length);
  range.setNumberFormat('@');
  range.setValues([line]);
  for (col = 0; col < line.length; col++) setCell_(spec.grid, row, col + 1, line[col]);
  if (row > spec.lastDataRow) spec.lastDataRow = row;
}

/** Where the next record goes: after the last row that holds anything. */
function nextRow_(ctx, spec) {
  return spec.lastDataRow + 1;
}

function ensureRoom_(sheet, row) {
  if (row <= sheet.getMaxRows()) return;
  sheet.insertRowsAfter(sheet.getMaxRows(), row - sheet.getMaxRows() + INVENTORY_ROW_BLOCK);
}

/** Group the columns being changed into as few sheet calls as their positions allow. */
function contiguousRuns_(spec, names) {
  var columns = [];
  for (var i = 0; i < names.length; i++) {
    var col = spec.columns[names[i]];
    if (col === undefined) {
      throw refusal('"' + names[i] + '" is not a column on ' + spec.title +
        '. This script is asking for something the schema does not have.');
    }
    columns.push(col + 1);
  }
  columns.sort(function (a, b) { return a - b; });
  var runs = [];
  for (i = 0; i < columns.length; i++) {
    if (runs.length && columns[i] === runs[runs.length - 1].lastCol + 1) {
      runs[runs.length - 1].lastCol = columns[i];
    } else {
      runs.push({ firstCol: columns[i], lastCol: columns[i] });
    }
  }
  return runs;
}


/* =========================================================================
 * Values: text, ids, times, numbers
 * ======================================================================= */

/**
 * One cell as the text the store would have written for it.
 *
 * Everything the server writes arrives as text, including dates and quantities, so text
 * is the format both writers agree on. A cell a person typed into directly may instead
 * hold a real date or number, which is rendered back the same way -- losing the locale
 * ambiguity that made it worth avoiding in the first place. A date with no time-of-day
 * reads as a date, because that is what whoever typed it meant.
 */
function text_(value) {
  if (value === null || value === undefined) return '';
  if (value instanceof Date) return stampFromDate_(value);
  if (typeof value === 'number') return numberToText_(value);
  return String(value).replace(/^[\s]+|[\s]+$/g, '');
}

function cellAt_(grid, row, col) {
  var line = grid[row - 1];
  if (!line) return '';
  return text_(line[col - 1]);
}

function setCell_(grid, row, col, value) {
  while (grid.length < row) grid.push([]);
  var line = grid[row - 1];
  while (line.length < col) line.push('');
  line[col - 1] = value;
}

function rowAt_(grid, row) {
  return grid[row - 1] || [];
}

function indexOf_(list, value) {
  for (var i = 0; i < list.length; i++) {
    if (list[i] === value) return i;
  }
  return -1;
}

function pairs_(rows) {
  var out = {};
  for (var i = 0; i < rows.length; i++) out[rows[i][0]] = rows[i][1];
  return out;
}

function column_(rows) {
  var out = [];
  for (var i = 0; i < rows.length; i++) out.push(rows[i][0]);
  return out;
}

/**
 * Read one published unit list back as a list. `(none)` is how the generator shows the
 * empty string, which is legal for a unitless assay and invisible in a cell -- so it
 * comes back out as the empty string it stands for.
 */
function splitList_(cell) {
  if (!cell) return [];
  var parts = cell.split(',');
  var out = [];
  for (var i = 0; i < parts.length; i++) {
    var part = parts[i].replace(/^[\s]+|[\s]+$/g, '');
    out.push(part === '(none)' ? '' : part);
  }
  return out;
}

/** `B12` -> {row: 12, col: 2}. Anything else is a broken map, said as such. */
function parseA1_(reference) {
  var match = /^([A-Za-z]+)([0-9]+)$/.exec(reference || '');
  if (!match) {
    throw failure('The interface map says a box is at "' + reference + '", which is not a cell.');
  }
  var col = 0;
  for (var i = 0; i < match[1].length; i++) {
    col = col * 26 + (match[1].charCodeAt(i) - 64);
  }
  return { row: Number(match[2]), col: col };
}

/**
 * Mint an id nobody has used yet, in this tab or earlier in this press.
 *
 * The alphabet and the length come off `options`, so this cannot drift from the server's
 * own shape. Math.random is enough here: an id is a surrogate key, not a secret, and the
 * only property that matters is not colliding -- 32 characters to the power of 8 is a
 * wide enough space that a collision is a thing to check for, not a thing to expect.
 *
 * Worth checking anyway, because the server does not: it mints blind and only notices a
 * collision afterwards, when the duplicate primary key refuses the write. A script that
 * just appended would keep the collision, since nothing re-reads the tab later.
 */
function mintId_(ctx, prefix, table) {
  var alphabet = ctx.constants['id_alphabet'];
  var length = Number(ctx.constants['id_suffix_length']);
  if (!alphabet || !length) {
    throw failure('The interface map does not say how ids are built.');
  }
  if (!ctx.minted) ctx.minted = {};
  var live = table ? table_(ctx, table) : null;
  for (var attempt = 0; attempt < 200; attempt++) {
    var suffix = '';
    for (var i = 0; i < length; i++) {
      suffix += alphabet.charAt(Math.floor(Math.random() * alphabet.length));
    }
    var candidate = prefix + '_' + suffix;
    if (ctx.minted[candidate]) continue;
    if (live && live.rowOf[candidate]) continue;
    ctx.minted[candidate] = true;
    return candidate;
  }
  throw refusal('Could not find a free id after 200 tries, which should not happen. ' +
    'Try again.');
}

/** UTC to the minute, always with the Z -- the format the server writes and expects. */
function nowStamp_() {
  return Utilities.formatDate(new Date(), 'UTC', "yyyy-MM-dd'T'HH:mm'Z'");
}

function stampFromDate_(date) {
  var zone = 'UTC';
  try {
    zone = SpreadsheetApp.getActiveSpreadsheet().getSpreadsheetTimeZone();
  } catch (ignored) {
    // Reading a hand-typed date without a spreadsheet around: UTC is the honest guess.
  }
  var clock = Utilities.formatDate(date, zone, 'HH:mm:ss');
  var pattern = (clock === '00:00:00') ? 'yyyy-MM-dd' : "yyyy-MM-dd'T'HH:mm'Z'";
  return Utilities.formatDate(date, zone, pattern);
}

function numberToText_(value) {
  if (value === Math.floor(value) && Math.abs(value) < 1e15) return String(value);
  return String(value);
}


/* =========================================================================
 * Checking what was typed
 * ======================================================================= */

/** The value of one input, by target. */
function askedFor_(ctx, target) {
  return ctx.values[target] || '';
}

/** The label of one input, so a refusal names the box rather than the column. */
function labelOf_(ctx, target) {
  for (var i = 0; i < ctx.inputs.length; i++) {
    if (ctx.inputs[i].target === target) return ctx.inputs[i].label;
  }
  return target;
}

function inputKind_(ctx, target) {
  for (var i = 0; i < ctx.inputs.length; i++) {
    if (ctx.inputs[i].target === target) return ctx.inputs[i].kind;
  }
  return '';
}

/** A required box left empty, said as the box it is. */
function requireText_(ctx, target) {
  var value = askedFor_(ctx, target);
  if (!value) {
    throw refusal(labelOf_(ctx, target) + ' is required and was left empty.');
  }
  return value;
}

/**
 * An enumerated value, checked against the vocabulary published for its column.
 *
 * Case is not forgiven. Every other writer here -- the server, and the dropdowns on this
 * very sheet -- is exact about status and class strings, and a script that quietly
 * corrected `Present` to `present` would leave the sheet accepting things the server
 * refuses.
 */
function requireEnum_(ctx, target) {
  var value = askedFor_(ctx, target);
  var allowed = ctx.enums[target];
  if (!allowed) {
    throw failure('Nothing on ' + INVENTORY_OPTIONS_TAB + ' gives the legal values for ' +
      target + '. Run `inventory-mcp ui` to rebuild it.');
  }
  if (!value) {
    if (isRequired_(ctx, target)) {
      throw refusal(labelOf_(ctx, target) + ' is required and was left empty. ' +
        'It takes one of: ' + allowed.join(', ') + '.');
    }
    return '';
  }
  if (indexOf_(allowed, value) < 0) {
    throw refusal(labelOf_(ctx, target) + ' "' + value + '" is not one of: ' +
      allowed.join(', ') + '. Nothing was written.');
  }
  return value;
}

function isRequired_(ctx, target) {
  for (var i = 0; i < ctx.inputs.length; i++) {
    if (ctx.inputs[i].target === target) return ctx.inputs[i].required;
  }
  var field = ctx.fields[target];
  return field ? field.required : false;
}

/**
 * A number, if anything was typed.
 *
 * The typed text is kept as typed. The server stringifies what it is handed without
 * normalising it -- `12.50` is stored as `12.50`, not `12.5` -- and a script that tidied
 * the spelling would make the same reading look different depending on which door it
 * came in through. Only the shape is checked here.
 */
function optionalNumber_(ctx, target) {
  var value = askedFor_(ctx, target);
  if (!value) return '';
  if (!/^-?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?$/.test(value)) {
    throw refusal(labelOf_(ctx, target) + ' "' + value + '" is not a number. ' +
      'Nothing was written.');
  }
  return value;
}

/**
 * A date or timestamp, if anything was typed.
 *
 * Accepted only in the shape the server writes, because that is the shape it reads back.
 * A cell that Sheets turned into a real date arrives already converted and is rendered
 * straight back out by text_; a person who typed 19/10/2026 gets told the shape instead
 * of getting a guess stored under someone else's locale.
 */
function optionalDate_(ctx, target) {
  var value = askedFor_(ctx, target);
  if (!value) return '';
  var kind = inputKind_(ctx, target);
  var pattern = (kind === 'timestamp')
    ? /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}Z$/
    : /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/;
  if (!pattern.test(value)) {
    throw refusal(labelOf_(ctx, target) + ' "' + value + '" is not in the shape ' +
      (kind === 'timestamp' ? 'YYYY-MM-DDTHH:MMZ (UTC, to the minute)' : 'YYYY-MM-DD') +
      '. Nothing was written.');
  }
  return value;
}

/**
 * A reference to something that has to exist. Returns the row it points at, or refuses,
 * naming what is missing rather than leaving a dangling reference for the server to find.
 */
function requireRow_(ctx, target, id, purpose) {
  var spec = ctx.foreignKeys[target];
  if (!spec) {
    throw failure('Nothing on ' + INVENTORY_OPTIONS_TAB + ' says where ' + target +
      ' points. Run `inventory-mcp ui` to rebuild it.');
  }
  var table = spec.split('.')[0];
  if (!id) {
    if (isRequired_(ctx, target)) {
      throw refusal(labelOf_(ctx, target) + ' is required and was left empty.');
    }
    return null;
  }
  var live = table_(ctx, table);
  var row = live.rowOf[id];
  if (!row) {
    throw refusal(id + ' is not on the ' + table + ' tab' + (purpose ? ', ' + purpose : '') +
      '. Nothing was written.');
  }
  return { table: table, row: row, id: id, values: recordAt_(ctx, table, row) };
}

/** One row of a tab, by sheet row, as {column: text}. */
function recordAt_(ctx, title, row) {
  var spec = table_(ctx, title);
  var values = {};
  for (var col = 0; col < spec.header.length; col++) {
    if (spec.header[col]) values[spec.header[col]] = cellAt_(spec.grid, row, col + 1);
  }
  return values;
}

/**
 * Find a row by its own primary key, rather than through a declared foreign key.
 *
 * The rename and notes boxes are given an id and nothing else -- no column names it,
 * because there is no column to name -- so the lookup starts from the prefix map instead.
 */
function requireById_(ctx, title, id, purpose) {
  var live = table_(ctx, title);
  // Shape first. "Nothing is called smp_aioooooo" is true but useless; the alphabet is
  // left out of ids precisely so that a transcribed o or l reads as a mistake rather
  // than as a missing row, and that only helps if it is said before the lookup fails.
  tableForId_(ctx, id);
  var row = live.rowOf[id];
  if (!row) {
    throw refusal('Nothing on the ' + title + ' tab is called ' + id +
      (purpose ? ', ' + purpose : '') + '. Nothing was written.');
  }
  return { table: title, row: row, id: id, values: recordAt_(ctx, title, row) };
}

/** Which table an id belongs to, decided by its prefix -- nothing else is ever written. */
function tableForId_(ctx, id) {
  var match = /^([a-z]{3})_([0-9a-z]+)$/.exec(id || '');
  if (!match) {
    throw refusal('"' + id + '" is not an id. Ids look like three lowercase letters, an ' +
      'underscore, then ' + (ctx.constants['id_suffix_length'] || 'eight') +
      ' characters from ' + (ctx.constants['id_alphabet'] || 'the inventory alphabet') + '.');
  }
  var known = ctx.prefixes[match[1]];
  if (!known) {
    throw refusal('No table in this database uses ids beginning "' + match[1] + '_". ' +
      'The prefixes in use are: ' + Object.keys(ctx.prefixes).sort().join(', ') + '.');
  }
  if (match[2].length !== Number(ctx.constants['id_suffix_length']) ||
      !matchesAlphabet_(match[2], ctx.constants['id_alphabet'])) {
    throw refusal('"' + id + '" has the right shape for a ' + known.what + ' id except the ' +
      'end: ' + ctx.constants['id_suffix_length'] + ' characters from ' +
      ctx.constants['id_alphabet'] + '. A mistyped id writes to the wrong row, so nothing ' +
      'was written.');
  }
  return known;
}

function matchesAlphabet_(value, alphabet) {
  for (var i = 0; i < value.length; i++) {
    if (alphabet.indexOf(value.charAt(i)) < 0) return false;
  }
  return true;
}

/** Is this row retired? A tombstoned item holds no position and answers to no new links. */
function isRetired_(record) {
  return !!record.deleted_at;
}


/* =========================================================================
 * Saying what happened
 * ======================================================================= */

/**
 * Write the outcome on the action's own Result line, next to the boxes it describes.
 *
 * Refusals belong there and nowhere else: a popup interrupts everyone sharing the sheet,
 * and a log entry nobody opens is not feedback. The previous result is overwritten on
 * every press, so a stale complaint cannot sit under a form that has since been fixed.
 */
function announce_(ctx, key, message) {
  var text = message === undefined || message === null ? '' : String(message);
  if (!ctx) {
    Logger.log('Lab inventory: ' + text);
    return;
  }
  var action = ctx.actions[key];
  if (!action || !action.result) {
    Logger.log('Lab inventory: ' + text);
    return;
  }
  var where = parseA1_(action.result);
  var range = ctx.start.getRange(where.row, where.col);
  range.setNumberFormat('@');
  range.setWrap(true);
  range.setValue(text);
  SpreadsheetApp.flush();
}

/** Empty the boxes, but only once the write actually happened. */
function clearInputs_(ctx) {
  for (var i = 0; i < ctx.inputs.length; i++) {
    var input = ctx.inputs[i];
    if (!askedFor_(ctx, input.target)) continue;
    var where = parseA1_(input.cell);
    ctx.start.getRange(where.row, where.col).setValue('');
  }
  SpreadsheetApp.flush();
}

/** Something the person pressing the button can act on. Not an error worth a stack trace. */
function refusal(message) {
  var problem = new Error(message);
  problem.isRefusal = true;
  return problem;
}

/** Something wrong with the interface itself, which the person cannot fix by typing. */
function failure(message) {
  return new Error(message);
}

/**
 * Script properties, fetched when first needed rather than at load time.
 *
 * Laziness is not a style choice here. `onOpen` runs as a simple trigger in
 * AuthMode.LIMITED, where services that need authorization are unavailable, and a
 * top-level `PropertiesService.getScriptProperties()` executes during script load --
 * so it would throw before any function ran and the Extensions menu would never appear,
 * in every workbook, with nothing on screen to explain why. Only code reached from a
 * click asks for this, and a click runs in FULL.
 */
function properties_() {
  return PropertiesService.getScriptProperties();
}


/* =========================================================================
 * The actions
 *
 * Each one proves what it is about to write before writing it, and says why in the
 * same words the server would use -- the wording below is deliberately close to
 * validation.py, so a person who has seen a refusal from either side recognises it.
 * Where the server's own message names a tool argument (`pass location_id=`), these say
 * the box instead, because whoever pressed the button is not calling a tool.
 * ======================================================================= */

var INVENTORY_HANDLERS_ = {
  add_sample: addSample_,
  add_supply: addSupply_,
  add_container: addContainer_,
  record_measurement: recordMeasurement_,
  move_item: moveItem_,
  set_status: setStatus_,
  rename_item: renameItem_,
  edit_notes: editNotes_,
  retire_item: retireItem_
};


/* -- wells, capacity, containment ---------------------------------------- */

/**
 * Split a well label the way the server splits it.
 *
 * `B7` is row B column 7, and `1A` is row A column 1 -- when both halves could be
 * either, the letters are the row. Two numbers is not a well at all.
 */
function splitWell_(position) {
  var parts = /^([A-Za-z]+|\d+)(\d+|[A-Za-z]+)$/.exec(position || '');
  if (!parts) return null;
  var first = parts[1];
  var second = parts[2];
  var firstIsNumber = /^[0-9]+$/.test(first);
  var secondIsNumber = /^[0-9]+$/.test(second);
  if (firstIsNumber && secondIsNumber) return null;
  if (firstIsNumber) return { row: second, col: first };
  return { row: first, col: second };
}

function intOrNull_(text) {
  if (!text || !/^-?[0-9]+$/.test(text)) return null;
  return Number(text);
}

/** Spreadsheet-style row labels: A..Z, AA, AB... */
function defaultRowLabels_(count) {
  var labels = [];
  for (var i = 1; i <= count; i++) {
    var label = '';
    var n = i;
    while (n > 0) {
      var remainder = (n - 1) % 26;
      label = String.fromCharCode(65 + remainder) + label;
      n = Math.floor((n - 1) / 26);
    }
    labels.push(label);
  }
  return labels;
}

function defaultColumnLabels_(count) {
  var labels = [];
  for (var i = 1; i <= count; i++) labels.push(String(i));
  return labels;
}

/**
 * The labels down one side of a container, or null when it has no grid.
 *
 * A `row_labels` override counts only when it lists exactly as many names as the
 * container claims to have rows. Anything else falls back to the default sequence,
 * silently -- which is what the server does, and the reason it is spelled out here:
 * a half-filled override list is a mislabeled box, not a licence to accept any row name.
 */
function axisLabels_(container, axis) {
  var count = intOrNull_(axis === 'rows' ? container.rows : container.cols);
  if (count === null || count <= 0) return null;
  var override = text_(axis === 'rows' ? container.row_labels : container.col_labels);
  if (override) {
    var named = override.split(/[,;|\s]+/);
    var parts = [];
    for (var i = 0; i < named.length; i++) {
      if (named[i]) parts.push(named[i]);
    }
    if (parts.length === count) return parts;
  }
  return axis === 'rows' ? defaultRowLabels_(count) : defaultColumnLabels_(count);
}

/** How the server describes an axis in a refusal: `A..M`, or every label spelled out. */
function describeAxis_(axis, labels) {
  var defaults = axis === 'rows' ? defaultRowLabels_(labels.length)
    : defaultColumnLabels_(labels.length);
  var same = defaults.length === labels.length;
  for (var i = 0; i < labels.length; i++) {
    if (defaults[i] !== labels[i]) same = false;
  }
  return same ? labels[0] + '..' + labels[labels.length - 1] : labels.join(', ');
}

function containsIgnoreCase_(labels, value) {
  var wanted = String(value).toUpperCase();
  for (var i = 0; i < labels.length; i++) {
    if (String(labels[i]).toUpperCase() === wanted) return true;
  }
  return false;
}

/**
 * Is that well a real well of this container? No check at all is made on a container
 * without a full grid, which is the server's behaviour too: a shelf has no wells, and
 * inventing a rule for its free-text location note would refuse things that are fine.
 */
function checkWell_(ctx, container, position) {
  if (!position) return;
  var split = splitWell_(position);
  if (!split) {
    throw refusal('Well "' + position + '" is not a well-formed well coordinate like \'B7\'.');
  }
  var rows = intOrNull_(container.rows);
  var cols = intOrNull_(container.cols);
  if (!rows || !cols) return;
  var rowLabels = axisLabels_(container, 'rows');
  var columnLabels = axisLabels_(container, 'cols');
  if (rowLabels && !containsIgnoreCase_(rowLabels, split.row)) {
    throw refusal('Well "' + position + '" uses row "' + split.row + '", which does not exist ' +
      'in ' + describeContainer_(container) + ' (rows are ' +
      describeAxis_('rows', rowLabels) + ')');
  }
  if (columnLabels && !containsIgnoreCase_(columnLabels, split.col)) {
    throw refusal('Well "' + position + '" uses column "' + split.col + '", which does not ' +
      'exist in ' + describeContainer_(container) + ' (columns are ' +
      describeAxis_('cols', columnLabels) + ')');
  }
}

function describeContainer_(container) {
  var label = text_(container.label);
  return label ? "'" + label + "'" : container.container_id;
}

/** Stated capacity, else the grid, else no limit. Mirrors db.capacity_of. */
function capacityOf_(container) {
  var stated = intOrNull_(container.capacity);
  // A stated zero is an answer -- nothing fits here -- not the absence of one, so it
  // has to be tested for rather than truthied. The grid below is different: a container
  // declared as 0 rows has no grid, which really is no information.
  if (stated !== null) return stated;
  var rows = intOrNull_(container.rows);
  var cols = intOrNull_(container.cols);
  if (rows && cols) return rows * cols;
  return null;
}

/** Live items and child containers directly inside a container. */
function occupantsOf_(ctx, containerId, excludeId) {
  var items = [];
  var records = records_(ctx, 'items');
  for (var i = 0; i < records.length; i++) {
    if (isRetired_(records[i])) continue;
    if (records[i].item_id === excludeId) continue;
    if (records[i].location_id === containerId) items.push(records[i]);
  }
  var containers = [];
  records = records_(ctx, 'containers');
  for (i = 0; i < records.length; i++) {
    if (records[i].container_id === excludeId) continue;
    if (records[i].parent_id === containerId) containers.push(records[i]);
  }
  return { items: items, containers: containers };
}

function requireSpace_(ctx, container, excludeId) {
  var capacity = capacityOf_(container);
  if (capacity === null) return;
  var inside = occupantsOf_(ctx, container.container_id, excludeId);
  var used = inside.items.length + inside.containers.length;
  if (used >= capacity) {
    throw refusal('no space in "' + text_(container.label) + '": ' + used + '/' + capacity +
      ' slots taken and this needs one.');
  }
}

/**
 * Is that well already someone else's? Case is folded, because `b7` and `B7` are one
 * place and the server treats them as such.
 */
function requireWellFree_(ctx, container, position, excludeId) {
  if (!position) return;
  var wanted = position.toUpperCase();
  var inside = occupantsOf_(ctx, container.container_id, excludeId);
  var claimants = [];
  for (var i = 0; i < inside.items.length; i++) {
    if (text_(inside.items[i].position).toUpperCase() === wanted) {
      claimants.push(inside.items[i].item_id);
    }
  }
  if (claimants.length) {
    throw refusal('well ' + position + ' of "' + text_(container.label) + '" is claimed by ' +
      (claimants.length + 1) + ' items: ' + claimants.concat([excludeId || 'this one']).join(', '));
  }
}

/** Everything below a container in the hierarchy, including itself. */
function containerSubtree_(ctx, containerId) {
  var children = {};
  var records = records_(ctx, 'containers');
  for (var i = 0; i < records.length; i++) {
    var parent = text_(records[i].parent_id);
    if (!parent) continue;
    if (!children[parent]) children[parent] = [];
    children[parent].push(text_(records[i].container_id));
  }
  var seen = {};
  var queue = [containerId];
  while (queue.length) {
    var current = queue.shift();
    if (seen[current]) continue;
    seen[current] = true;
    var kids = children[current] || [];
    for (i = 0; i < kids.length; i++) {
      if (!seen[kids[i]]) queue.push(kids[i]);
    }
  }
  return seen;
}

/**
 * Look a container up by id and refuse if it is not there or cannot hold anything.
 *
 * By primary key rather than through the foreign-key map: `container_id` is what the
 * other columns point at, so it is never itself listed as a foreign key.
 */
function requireContainer_(ctx, id, purpose) {
  if (!id) return null;
  var found = requireById_(ctx, 'containers', id, purpose);
  return found.values;
}

/** Park something in a container, checking the well, the space and the slot. */
function requirePlaceable_(ctx, container, position, excludeId) {
  checkWell_(ctx, container, position);
  requireSpace_(ctx, container, excludeId);
  requireWellFree_(ctx, container, position, excludeId);
}


/* -- where a thing goes -------------------------------------------------- */

/**
 * The box and well someone asked for, checked as a pair.
 *
 * A well without a box is the error the server calls position_without_location, and it
 * is worth refusing here rather than storing: a floating `B7` reads as a real place to
 * the next person and as nothing at all to the validator until it becomes an error.
 */
function askedPlace_(ctx, boxTarget, wellTarget, purpose) {
  var boxId = askedFor_(ctx, boxTarget);
  var position = askedFor_(ctx, wellTarget);
  if (position && !boxId) {
    throw refusal('Well "' + position + '" is recorded but no box is set. Name the container ' +
      'it is a well of, or clear the well.');
  }
  var container = requireContainer_(ctx, boxId, purpose);
  return { id: boxId, container: container, position: position };
}

/** An assay named by its code, as the published table describes it. */
function resolveAssay_(ctx, code, purpose) {
  if (!code) return null;
  var assay = ctx.assays[code.toLowerCase()];
  if (!assay) {
    var known = Object.keys(ctx.assays);
    throw refusal('"' + code + '" is not an assay code on the assays tab' +
      (purpose ? ', ' + purpose : '') + '. Registered: ' + (known.length ? known.join(', ') : 'none') +
      '.');
  }
  return assay;
}

/**
 * Which reading this one retires, when nobody said.
 *
 * Copied predicate, not an improvement on it: the bottom-most row for that item and
 * assay that nothing points at, in sheet order. Sorting by measured_on instead splits a
 * same-minute re-measurement apart, because readings are stamped to the minute.
 */
function currentReadingId_(ctx, itemId, assayId) {
  var readings = records_(ctx, 'measurements');
  var retired = retiredReadingIds_(readings);
  var current = '';
  for (var i = 0; i < readings.length; i++) {
    if (text_(readings[i].item_id) !== itemId) continue;
    if (text_(readings[i].assay_id) !== assayId) continue;
    var id = text_(readings[i].measurement_id);
    if (!id || retired[id]) continue;
    current = id;
  }
  return current;
}

/** The projects a participant is enrolled in, exactly -- nesting does not inherit. */
function enrolledProjectsOf_(ctx, patientId) {
  var projects = {};
  var links = records_(ctx, 'patient_projects');
  for (var i = 0; i < links.length; i++) {
    if (text_(links[i].patient_id) === patientId) {
      projects[text_(links[i].project_id)] = true;
    }
  }
  return projects;
}

/**
 * The pairing rule, in the sheet's own words.
 *
 * The server refuses a sample whose project is not one the participant is enrolled in,
 * and does not widen enrolment through sub-projects -- inheriting it downward would let
 * someone broaden a participant's scope by filing a project underneath them.
 */
function requireEnrolment_(ctx, patientId, projectId) {
  if (!patientId || !projectId) return;
  var enrolled = enrolledProjectsOf_(ctx, patientId);
  if (enrolled[projectId]) return;
  throw refusal('that participant is not enrolled in ' + projectId +
    ' -- enrolment does not inherit from a parent project, so record the pair on the ' +
    'patient_projects tab first.');
}

/**
 * Stamp a write with who pressed and when -- if the tab has anywhere to put it.
 *
 * Extension tabs and readings have no audit columns, and asking for one there would be a
 * write to a column that does not exist. The same rule the server follows: refresh the
 * audit trail on tables that carry one, leave the others alone.
 */
function stamped_(ctx, title, values) {
  var spec = table_(ctx, title);
  var stamped = {};
  var names = Object.keys(values);
  for (var i = 0; i < names.length; i++) stamped[names[i]] = values[names[i]];
  if (spec.columns.hasOwnProperty('updated_by')) stamped.updated_by = ctx.actor;
  if (spec.columns.hasOwnProperty('updated_at')) stamped.updated_at = nowStamp_();
  return stamped;
}

/* -- the nine actions ---------------------------------------------------- */

function addSample_(ctx) {
  var name = requireText_(ctx, 'items.name');
  var sampleType = requireEnum_(ctx, 'samples.sample_type');
  var place = askedPlace_(ctx, 'items.location_id', 'items.position',
    'the box this sample goes in');
  if (place.container) requirePlaceable_(ctx, place.container, place.position, null);

  var patient = requireRow_(ctx, 'samples.patient_id', askedFor_(ctx, 'samples.patient_id'),
    'the participant this came from');
  var project = requireRow_(ctx, 'samples.project_id', askedFor_(ctx, 'samples.project_id'),
    'the project it belongs to');
  requireEnrolment_(ctx, patient && patient.id, project && project.id);

  var collected = optionalDate_(ctx, 'samples.collected_on');
  var handler = askedFor_(ctx, 'items.handler') || ctx.actor;
  var itemId = mintId_(ctx, ctx.classes.sample.prefix, 'items');

  appendRecord_(ctx, 'items', {
    item_id: itemId, class: 'sample', name: name, status: 'present',
    location_id: place.id, position: place.position, handler: handler,
    notes: askedFor_(ctx, 'items.notes'), deleted_at: ''
  });
  // The extension row is written even when every field in it is blank. Without it the
  // item is an error everywhere, not just here -- missing_extension blocks every later
  // write that touches items.
  appendRecord_(ctx, 'samples', {
    item_id: itemId, gml_id: '', patient_id: patient ? patient.id : '',
    project_id: project ? project.id : '', sample_type: sampleType,
    source_material: askedFor_(ctx, 'samples.source_material'), collected_on: collected,
    received_on: '', processed_on: ''
  });
  return 'Added ' + itemId + ' (' + name + ')' + describePlace_(place) + '.';
}

function addSupply_(ctx) {
  var itemClass = askedFor_(ctx, 'items.class');
  if (!itemClass) {
    throw refusal('Class is required and was left empty. It takes one of: ' +
      ctx.supplyClasses.join(', ') + '.');
  }
  if (indexOf_(ctx.supplyClasses, itemClass) < 0) {
    throw refusal('"' + itemClass + '" is not something this block makes. It takes one of: ' +
      ctx.supplyClasses.join(', ') + '. A tube of material is a sample, made above.');
  }
  var names = ctx.classes[itemClass];
  if (!names) {
    throw failure('The interface map has no prefix for the class "' + itemClass + '".');
  }
  var name = requireText_(ctx, 'items.name');
  var place = askedPlace_(ctx, 'items.location_id', 'items.position',
    'the box this supply goes in');
  if (place.container) requirePlaceable_(ctx, place.container, place.position, null);

  var assayCode = askedFor_(ctx, 'supply.assay_code');
  var quantity = optionalNumber_(ctx, 'supply.quantity');
  var unit = askedFor_(ctx, 'supply.unit');
  if (quantity && !assayCode) {
    throw refusal('An opening quantity needs a Quantity assay to be a reading of. Name its ' +
      'code, or clear the quantity to register the item without a reading.');
  }
  var assay = resolveAssay_(ctx, assayCode, 'for the opening quantity');
  if (assay && !quantity) {
    throw refusal('A Quantity assay was named with no opening quantity. Give the number, or ' +
      'clear the assay -- naming one alone records nothing.');
  }
  if (assay && assay.resultType !== 'numeric') {
    throw refusal('assay "' + assayCode + '" is qualitative, so it cannot hold an opening ' +
      'quantity. Use a numeric assay, or clear both boxes.');
  }
  var measuredUnit = unit;
  if (assay) {
    if (!unit && indexOf_(assay.units, '') >= 0) {
      measuredUnit = assay.canonicalUnit || 'count';
    }
    if (!measuredUnit) {
      throw refusal('assay "' + assayCode + '" needs a unit (its canonical unit is ' +
        (assay.canonicalUnit || '?') + ').');
    }
    if (indexOf_(assay.units, measuredUnit) < 0) {
      throw refusal('unit "' + measuredUnit + '" is not a ' + assay.quantity + ' unit. ' +
        'It fits: ' + (assay.units.filter(function (u) { return u; }).join(', ') || 'none') + '.');
    }
  } else if (unit) {
    throw refusal('A unit was given with no Quantity assay to say what kind of thing it ' +
      'measures.');
  }

  var itemId = mintId_(ctx, names.prefix, 'items');
  appendRecord_(ctx, 'items', {
    item_id: itemId, class: itemClass, name: name, status: 'present',
    location_id: place.id, position: place.position,
    handler: askedFor_(ctx, 'items.handler') || ctx.actor,
    notes: askedFor_(ctx, 'items.notes'), deleted_at: ''
  });
  appendRecord_(ctx, names.extension, {
    item_id: itemId, price: optionalNumber_(ctx, 'supply.price'),
    expires_on: optionalDate_(ctx, 'supply.expires_on'),
    storage_requirements: askedFor_(ctx, 'supply.storage_requirements')
  });
  if (assay) {
    // The opening reading is append-only like any other, and supersedes nothing: it is
    // the first, not a correction.
    appendRecord_(ctx, 'measurements', {
      measurement_id: mintId_(ctx, ctx.prefixes.msm.prefix, 'measurements'),
      item_id: itemId, assay_id: assay.assayId, measured_on: nowStamp_(),
      value_num: quantity, value_text: '', unit: measuredUnit,
      handler: askedFor_(ctx, 'items.handler') || ctx.actor, instrument: '',
      supersedes: '', notes: 'initial quantity at registration'
    });
  }
  return 'Added ' + itemId + ' (' + name + ')' + describePlace_(place) +
    (assay ? ', opened at ' + quantity + ' ' + measuredUnit : '') + '.';
}

function addContainer_(ctx) {
  var label = requireText_(ctx, 'containers.label');
  var kind = requireEnum_(ctx, 'containers.kind');
  var inside = askedFor_(ctx, 'containers.parent_id');
  var parent = requireContainer_(ctx, inside, 'the container holding this one');

  var rows = optionalNumber_(ctx, 'containers.rows');
  var cols = optionalNumber_(ctx, 'containers.cols');
  var capacity = optionalNumber_(ctx, 'containers.capacity');
  var geometry = checkGeometry_(rows, cols, capacity, kind);

  var containerId = mintId_(ctx, ctx.prefixes.ctr.prefix, 'containers');
  // create_container on the server leaves the audit columns blank; filling them here
  // keeps the button's own edits attributable, which is the one thing a button can add
  // over typing (stamped_ does it, and every other container write sets them too).
  appendRecord_(ctx, 'containers', {
    container_id: containerId, parent_id: inside, kind: kind, label: label,
    rows: rows, cols: cols, row_labels: '', col_labels: '', capacity: capacity,
    notes: askedFor_(ctx, 'containers.notes')
  });
  return 'Registered ' + containerId + ' (' + label + ')' +
    (parent ? ' inside ' + describeContainer_(parent) : '') + '.' +
    (geometry ? '\n' + geometry : '');
}

function recordMeasurement_(ctx) {
  var item = requireRow_(ctx, 'measurements.item_id', askedFor_(ctx, 'measurements.item_id'),
    'the tube this reading belongs to');
  if (!item) throw refusal('Item is required and was left empty.');
  if (isRetired_(item.values)) {
    throw refusal(item.id + ' was retired, and a new reading would be filed against a ' +
      'tombstone. Retire the reading on a live item instead.');
  }
  var assay = resolveAssay_(ctx, askedFor_(ctx, 'supply.assay_code'),
    'name the assay this reading is of');
  if (!assay) throw refusal('Assay is required and was left empty. Name its code, like qubit_dsdna.');

  var numeric = askedFor_(ctx, 'measurements.value_num');
  var textual = askedFor_(ctx, 'measurements.value_text');
  var unit = askedFor_(ctx, 'measurements.unit');
  if (numeric && textual) {
    throw refusal('A reading is either a number or a piece of text, not both. Clear one of ' +
      'the two.');
  }
  if (assay.resultType === 'numeric') {
    if (!numeric) throw refusal('assay "' + assay.name + '" is numeric, so it needs a Value.');
    if (!unit && indexOf_(assay.units, '') >= 0) unit = assay.canonicalUnit || 'count';
    if (!unit) {
      throw refusal('assay "' + assay.name + '" needs a unit (its canonical unit is ' +
        (assay.canonicalUnit || '?') + ').');
    }
    if (indexOf_(assay.units, unit) < 0) {
      throw refusal('unit "' + unit + '" is not a ' + assay.quantity + ' unit. It fits: ' +
        (assay.units.filter(function (u) { return u; }).join(', ') || 'none') + '.');
    }
    textual = '';
  } else {
    if (!textual) {
      throw refusal('assay "' + assay.name + '" is qualitative, so it needs Result text ' +
        'rather than a number.');
    }
    if (numeric) throw refusal('assay "' + assay.name + '" is qualitative, so it takes text, ' +
      'not a number.');
    numeric = '';
    unit = '';
  }

  var when = optionalDate_(ctx, 'measurements.measured_on') || nowStamp_();
  var readings = records_(ctx, 'measurements');
  var replaces = askedFor_(ctx, 'measurements.supersedes');
  var notice = '';
  if (replaces) {
    var replaced = requireRow_(ctx, 'measurements.supersedes', replaces,
      'the reading this one replaces');
    if (!replaced) throw refusal('Corrects is required and was left empty.');
    if (text_(replaced.values.item_id) !== item.id ||
        text_(replaced.values.assay_id) !== assay.assayId) {
      throw refusal(replaces + ' is a reading of something else, so retiring it here would ' +
        'leave the wrong reading uncounted.');
    }
    if (retiredReadingIds_(readings)[replaces]) {
      throw refusal(replaces + ' is already superseded.');
    }
  } else {
    replaces = currentReadingId_(ctx, item.id, assay.assayId);
    if (replaces) {
      notice = '\n' + replaces + ' was the current ' + (assay.code || assay.name) +
        ' reading for ' + item.id + '; this one retires it.';
    }
  }

  var measurementId = mintId_(ctx, ctx.prefixes.msm.prefix, 'measurements');
  appendRecord_(ctx, 'measurements', {
    measurement_id: measurementId, item_id: item.id, assay_id: assay.assayId,
    measured_on: when, value_num: numeric, value_text: textual, unit: unit,
    handler: askedFor_(ctx, 'measurements.handler') || ctx.actor,
    instrument: askedFor_(ctx, 'measurements.instrument'), supersedes: replaces,
    notes: askedFor_(ctx, 'measurements.notes')
  });
  // Stored exactly as observed, in the unit it was observed in. Conversion to the
  // assay's canonical unit happens when a reading is read, never when it is written --
  // converting here would make the sheet's number mean something other than what the
  // instrument said, and the reader would convert it a second time.
  return 'Recorded ' + measurementId + ': ' + (numeric || textual) +
    (unit ? ' ' + unit : '') + ' for ' + item.id + '.' + notice;
}

/** Every reading that something else already retires. */
function retiredReadingIds_(readings) {
  var retired = {};
  for (var i = 0; i < readings.length; i++) {
    var pointed = text_(readings[i].supersedes);
    if (pointed) retired[pointed] = true;
  }
  return retired;
}

function moveItem_(ctx) {
  var subject = askedFor_(ctx, 'move.what');
  var intoId = askedFor_(ctx, 'move.into');
  var position = askedFor_(ctx, 'items.position');
  if (!subject) throw refusal('What is required and was left empty. Name an item or a container.');
  if (!intoId) throw refusal('Into is required and was left empty. Name the container to move it into.');

  var known = tableForId_(ctx, subject);
  var into = requireContainer_(ctx, intoId, 'the container to move it into');

  if (known.table === 'containers') {
    if (position) {
      throw refusal('Well is for items; a container takes a slot in its parent, not a well.');
    }
    var moving = requireById_(ctx, 'containers', subject, 'the container to move');
    if (subject === intoId) {
      throw refusal('cannot move ' + describeContainer_(moving.values) + ' into itself.');
    }
    var below = containerSubtree_(ctx, subject);
    if (below[intoId]) {
      throw refusal('cannot move ' + describeContainer_(moving.values) + ' into ' +
        describeContainer_(into) + ': that is already inside it.');
    }
    requireSpace_(ctx, into, subject);
    patchRecord_(ctx, 'containers', moving.row, { parent_id: intoId });
    return 'Moved ' + describeContainer_(moving.values) + ' into ' +
      describeContainer_(into) + '.';
  }

  var item = requireById_(ctx, 'items', subject, 'the item to move');
  if (isRetired_(item.values)) {
    throw refusal(subject + ' was deleted and remains only as a tombstone for provenance; ' +
      'it is not moved.');
  }
  if (text_(item.values.status) === 'consumed') {
    throw refusal(subject + ' is consumed and holds no slot; put it back to present with a ' +
      'box first.');
  }
  // An empty Well box cannot tell "clear it" from "leave it alone", so the same question
  // the server answers: still in the same container means it stays where it was. Losing
  // the well of a tube because someone re-picked its box is the worse mistake here. To
  // genuinely clear one, move it elsewhere and back, or edit the cell.
  var wanted = position;
  if (!wanted && intoId === text_(item.values.location_id)) {
    wanted = text_(item.values.position);
  }
  checkWell_(ctx, into, wanted);
  requireSpace_(ctx, into, subject);
  requireWellFree_(ctx, into, wanted, subject);
  patchRecord_(ctx, 'items', item.row, { location_id: intoId, position: wanted });
  return 'Moved ' + subject + ' into ' + describeContainer_(into) +
    (wanted ? ' at ' + wanted : ' (no well)') + '.';
}

function setStatus_(ctx) {
  var itemId = askedFor_(ctx, 'items.item_id');
  var status = requireEnum_(ctx, 'items.status');
  if (!itemId) throw refusal('Item is required and was left empty.');
  var item = requireById_(ctx, 'items', itemId, 'the item to change');
  if (isRetired_(item.values)) {
    throw refusal(itemId + ' was deleted and remains only as a tombstone for provenance; ' +
      'its status is not changed.');
  }

  var boxId = askedFor_(ctx, 'items.location_id');
  var position = askedFor_(ctx, 'items.position');
  var values = { status: status };
  var where = '';

  if (status === 'consumed') {
    if (boxId) {
      throw refusal('a consumed item holds no slot, so it takes no box. Leave Box empty, or ' +
        'mark the item present to put it somewhere.');
    }
    // Nothing remembers the well it vacates. That is deliberate in the schema as well as
    // here, so the result line says it out loud rather than letting it be discovered.
    values.location_id = '';
    values.position = '';
  } else if (boxId) {
    var container = requireContainer_(ctx, boxId, 'where the item should be');
    var stays = boxId === text_(item.values.location_id) && !position;
    var wanted = position || (stays ? text_(item.values.position) : '');
    checkWell_(ctx, container, wanted);
    requireSpace_(ctx, container, itemId);
    requireWellFree_(ctx, container, wanted, itemId);
    values.location_id = boxId;
    values.position = wanted;
    where = ' into ' + describeContainer_(container) + (wanted ? ' at ' + wanted : '');
  } else {
    if (position) {
      throw refusal('Well "' + position + '" is recorded but no box is set. Name the ' +
        'container it is a well of, or clear the well.');
    }
    if (!text_(item.values.location_id) && !text_(item.values.position)) {
      throw refusal(itemId + ' holds no slot, and "' + status + '" needs one: name a box. ' +
        'Consuming drops the location and nothing remembers it.');
    }
  }
  patchRecord_(ctx, 'items', item.row, values);
  return itemId + ' is now ' + status + '.' +
    (status === 'consumed' ? ' Its box and well are cleared, and nothing remembers them.' : where);
}

function renameItem_(ctx) {
  var target = editTarget_(ctx, 'any.id', 'nameColumn', 'rename');
  var replacement = requireText_(ctx, 'any.name');
  patchRecord_(ctx, target.table, target.row, namedValue_(target.column, replacement));
  return 'Renamed ' + target.id + ' to "' + replacement + '".';
}

function editNotes_(ctx) {
  var target = editTarget_(ctx, 'any.id', 'notesColumn', 'add a note to');
  var notes = askedFor_(ctx, 'any.notes');
  patchRecord_(ctx, target.table, target.row, namedValue_(target.column, notes));
  return notes
    ? 'Noted ' + target.id + '.'
    : 'Cleared the note on ' + target.id + '. The old text is not kept anywhere.';
}

function retireItem_(ctx) {
  var itemId = askedFor_(ctx, 'items.item_id');
  if (!itemId) throw refusal('Item is required and was left empty.');
  var item = requireById_(ctx, 'items', itemId, 'the item to retire');
  if (isRetired_(item.values)) throw refusal(itemId + ' is already retired.');
  // The row stays. Its measurements, its links and its place in someone's notebook all
  // keep pointing at it, which is the entire point of a tombstone over a deletion.
  patchRecord_(ctx, 'items', item.row, {
    deleted_at: nowStamp_(), location_id: '', position: ''
  });
  return 'Retired ' + itemId + '. Its readings and links are untouched; its box and well ' +
    'are free.';
}

/** Resolve an id for the two actions that take any id at all. */
function editTarget_(ctx, idTarget, whichColumn, verb) {
  var id = askedFor_(ctx, idTarget);
  if (!id) throw refusal('Id is required and was left empty.');
  var known = tableForId_(ctx, id);
  var column = known[whichColumn];
  if (!column) {
    throw refusal('The ' + known.table + ' tab has no ' +
      (whichColumn === 'nameColumn' ? 'name' : 'notes') + ' column, so there is nothing to ' +
      verb + '.');
  }
  var found = requireById_(ctx, known.table, id);
  if (isRetired_(found.values)) {
    throw refusal(id + ' was deleted and remains only as a tombstone for provenance; its ' +
      'text is not edited.');
  }
  return { table: known.table, row: found.row, id: id, column: column };
}

/** One column's worth of a patch, keyed by the column's actual name on the tab. */
function namedValue_(column, value) {
  var values = {};
  values[column] = value;
  return values;
}

function describePlace_(place) {
  if (!place.id) return ', not in a box yet';
  return ' in ' + describeContainer_(place.container) +
    (place.position ? ' at ' + place.position : '');
}

/**
 * Geometry stated for a new container, checked before it is written.
 *
 * Only the ones that make a container unusable stop it; the rest come back as notes on
 * the Result line, because a rack declared with a grid is worth flagging and still worth
 * having in the database.
 */
function checkGeometry_(rows, cols, capacity, kind) {
  var notes = [];
  var rowCount = intOrNull_(rows);
  var colCount = intOrNull_(cols);
  var capCount = intOrNull_(capacity);
  if (capacity && (capCount === null || capCount <= 0)) {
    throw refusal('Capacity must be a whole number greater than zero.');
  }
  if ((rows && (rowCount === null || rowCount <= 0)) || (cols && (colCount === null || colCount <= 0))) {
    throw refusal('Rows and Columns must be whole numbers greater than zero.');
  }
  if (rows && !cols) notes.push('Only Rows is set; neither is used without the other.');
  if (cols && !rows) notes.push('Only Columns is set; neither is used without the other.');
  if (rowCount && colCount) {
    if (rowCount * colCount > 2000) {
      notes.push('A ' + rowCount + 'x' + colCount + ' grid exceeds 2000 wells; check for a typo.');
    }
    if (kind !== 'box') {
      notes.push("'" + kind + "' declares a " + rowCount + 'x' + colCount + ' well grid; grids ' +
        'mean wells and belong on a box. If this holds nested containers, record them as ' +
        'capacity instead.');
    }
    if (capCount && capCount !== rowCount * colCount) {
      notes.push('Capacity ' + capCount + ' contradicts the ' + rowCount + 'x' + colCount +
        ' grid (' + (rowCount * colCount) + ' wells); the stated capacity wins.');
    }
  }
  return notes.join('\n');
}




