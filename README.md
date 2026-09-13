# Project Notes

Project Notes is a desktop application for organizing projects, tasks, and rich-text notes. It is built with Python, PyQt6, and SQLite, and keeps application data on the local machine.

## Features

- Create, rename, hide, review, and manually order projects.
- Create, edit, duplicate, move, bulk-add, and complete tasks.
- Pin tasks, mark waiting items and minutes of meeting, and view pinned tasks across projects.
- Assign due dates, snooze tasks, estimate effort, and configure daily, weekly, monthly, or completion-based recurrence.
- Add stakeholders and search across projects, tasks, notes, and stakeholder names.
- Filter completed, pinned, missing-due-date, and missing-effort tasks.
- Write rich-text notes with formatting, lists, highlighting, and pasted or dropped images.
- Crop, resize, copy, and remove images from notes.
- Import and export tasks as CSV, or copy and export the filtered task list as text.
- Create and switch between multiple SQLite databases.
- Run periodic project reviews.

An optional bundled addon adds project deep links and PARA folder associations. It can be enabled from the application's preferences and takes effect after restart.

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
