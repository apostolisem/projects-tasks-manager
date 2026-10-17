# Project Notes

Project Notes is a desktop application for organizing projects, tasks, and rich-text notes. It is built with Python, PyQt6, and SQLite, and keeps application data on the local machine.

## Interface

The window has three panes under a top bar with the database switcher and search:

- **Sidebar**: smart views (Due & overdue, Focus, Waiting, Meeting notes, No due date) with live counts, your projects, and a collapsible list of hidden projects.
- **Task list**: tasks grouped by status (Meeting notes, Focus, Next up, Waiting, Completed), a quick-add field, and an Auto / Manual switch for drag-and-drop ordering.
- **Task detail**: title, Complete / Focus / Waiting / MoM buttons, due date, repeat, stakeholders and rich-text notes. Full view hides the other two panes.

Completing, deleting or moving a task and deleting a project show a notice with **Undo**.

## Features

- Create, rename, hide, review, and manually order projects.
- Create, edit, duplicate, move, bulk-add, and complete tasks.
- Mark tasks as Focus, waiting or minutes of meeting, and see them across projects in the smart views.
- Assign due dates, snooze tasks, and configure daily, weekly, monthly, or completion-based recurrence.
- Add stakeholders and search across projects, tasks, notes, and stakeholder names (`@name`).
- Find tasks that have no due date in the No due date view.
- Write rich-text notes with formatting, headings, lists, checklists, links, highlighting, and pasted or dropped images.
- Type `- `, `1. `, `# ` or `[] ` at the start of a line for a list, heading or checklist item; find text in a note with Ctrl+F.
- Crop, resize, copy, and remove images from notes.
- Import and export tasks as CSV, or copy and export the filtered task list as text.
- Create and switch between multiple SQLite databases.
- Run periodic project reviews.

## Search

Every word must match somewhere in the project name, task title, or notes. Matching ignores case and accents, including Greek accents and final sigma; `%` and `_` are literal characters. Results emphasize title matches and show a highlighted notes excerpt when a word matches only in the notes. Hidden projects are labelled in the results.

Use `"exact phrase"`, `-word` (or `-"exact phrase"`), `@name`, and `in:project` (or `in:"Project name"`) to narrow results. `is:done` and `is:open` override the Completed toggle. Multiple `@name` or `in:project` filters match any of the supplied names.

Typing waits 150 ms before searching. Focus or click the empty search field, or press Down in it, to choose a recent search.

## Task details

The project name opens its project while keeping the task selected. Complete stays prominent; Focus and Waiting are quieter toggles. Meeting notes appears in More, and remains visible in the header when active. Due, Repeat, and People use compact controls; open Due for Today, Tomorrow, Next week, a calendar, or Clear.

People accept full names and suggest existing entries. Enter, comma, or semicolon confirms a name; spaces remain part of the name. Existing `@name` identifiers still work. Backspace in an empty input selects the last person; Delete then removes that person. Search full names with `@"John Smith"`.

The notes toolbar keeps common actions visible, groups formatting under Text style and insertion under Insert, and puts excess actions in More formatting at narrow widths. Controls are keyboard accessible without losing the note selection. The footer separates save status from the last-edited timestamp.

## Notes and images

Notes autosave after a short pause. HTML, searchable text, and attachment metadata are committed together. A failed save keeps the draft open and shows **Retry**, **Export**, and **Discard**; navigation and closing cannot silently abandon it. Export produces an HTML file with embedded images. Discard asks for confirmation.

Select an image to show Copy, Cut, Remove, Crop, Fit width, Actual size, Restore original proportions, and a percentage control. Percentage changes preserve the current proportions; Fit width shrinks oversized images without enlarging smaller ones. Resizing changes display dimensions while preserving the original pixels.

Drag resize shows a live image preview, width/height fields, a **Lock ratio** toggle, and **Apply**/**Cancel** controls. The numeric fields and Fit width remain available even when the corner handle is off-screen. Shift temporarily unlocks proportions while dragging. With focus in the note, Enter applies and Escape cancels; editing the note cancels the preview. Text reflows when the resize is applied. Applying unchanged dimensions does not add an Undo step. Cropping creates a separate asset. Both operations support Undo and leave other notes' images unchanged.

Copy/paste keeps mixed text and images together and imports embedded images into the database's attachment folder. External rich text is cleaned of source fonts and colours. An image that cannot be imported produces an error instead of silently replacing the selection. Tab and Shift+Tab indent/outdent paragraphs and list items without flattening their formatting or removing images.

Find in note uses the same case/accent matching as global search and updates its count while editing. Global search matches use temporary yellow highlighting and have Previous/Next controls in the note. A note that stops matching while being edited stays visible with a label until you leave it. Returning to a task restores the note's cursor and scroll position for the current session.

## Backups and restoration

On exit, pending note edits are saved before the SQLite backup is made. Each new backup has a companion directory named `<backup filename>.assets`, containing its image snapshot and `restore.json`. Keep the database backup and this directory together. The latest 30 database backups and their companion directories are retained.

To restore into an empty directory:

1. Copy the backup database there using the original filename recorded in `restore.json`.
2. Copy the attachment folders inside the `.assets` directory beside that database.
3. Open the restored database from the app's database switcher.

These complete image snapshots apply to newly created backups; older database-only backups still require their original attachments. Missing images or a failed backup are reported, and closing without that backup requires confirmation. Original crop assets are retained so that Undo remains safe; image resizing does not create additional pixel copies.

## Keyboard shortcuts

| Shortcut | Action |
|---|---|
| Ctrl+K | Focus search (Esc clears it) |
| N, Ctrl+N or Ctrl+S | Add a task |
| ↑ / ↓ or J / K | Move through tasks |
| Ctrl+Enter | Complete or reopen the selected task |
| Delete | Delete the selected task |
| F2 or double-click | Rename a project or task |
| Esc | Leave full view |
| Ctrl+L / Ctrl+Shift+C / Ctrl+F | In notes: link, tick checklist item, find |

N, J, K, the arrow keys and Delete apply when the cursor is not in a text field.

## Requirements

- Python 3.9 or newer
- PyQt6
- Pillow
- psutil

Install the Python dependencies from the project directory:

```bash
python3 -m pip install -r requirements.txt
```

## Run

```bash
python3 app.pyw
```

On first launch, the application creates local storage as needed. Use **File → New Database…** or **File → Open Database…** to work with another SQLite database.

## License

This project is licensed under the GNU General Public License v3.0. See [LICENSE](LICENSE).

The bundled Barlow and Barlow Condensed fonts are licensed under the SIL Open Font License (see [fonts/OFL.txt](fonts/OFL.txt)). The icons in `icons/lucide/` are from Lucide (ISC License).
