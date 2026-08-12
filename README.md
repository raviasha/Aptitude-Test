# Aptitude Lab — Server Installation

The Windows installer installs a single server executable, creates a Desktop shortcut, and opens the application at `http://localhost:8000`. Student computers use `http://<SERVER-LAN-IP>:8000`.

## Assessment files

After installation, copy each assessment pair into:

```text
C:\ProgramData\Aptitude Lab\Question Banks
```

Each pair has the same base filename:

```text
placement-set-02.html
placement-set-02.json
```

- The HTML file contains student-facing text, tables, and inline SVG graphs/diagrams.
- The JSON file holds options, correct answers, category, difficulty, and explanation.

Open Faculty → **Question banks**, refresh the folder, and click **Import**. The app stores both the visual question markup and scoring data in SQLite; students never receive correct answers.

The installer copies a visual graph sample pair into the folder on first startup.

## Build the Windows installer

Run [build-windows.bat](/Users/rampetaravishankar/Documents/New%20project/build-windows.bat) on a Windows computer with Python 3.10+ and Inno Setup 6 installed. It produces:

```text
release\Aptitude-Lab-Setup.exe
```

Install that file on the designated lab server. The installer adds a private-network Windows Firewall rule for port 8000 and places the Desktop shortcut.

## Demo accounts

- Student: `1KS23AI042` / `student123`
- Faculty: `faculty` / `faculty123`

Change demo passwords and set a strong `SESSION_SECRET` before production use.
