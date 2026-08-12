# Build and Install the Windows Server EXE

Use these steps on a Windows computer to build the Aptitude Lab installer.

## 1. Install prerequisites

Install the following tools:

1. **Python 3.10 or later** from [python.org](https://www.python.org/downloads/windows/).
   During installation, select **Add Python to PATH**.
2. **Inno Setup 6** from [jrsoftware.org](https://jrsoftware.org/isdl.php).

## 2. Build the installer

Clone or download this repository. Open Command Prompt in the project folder, then run:

```bat
build-windows.bat
```

The script creates a Python build environment, installs the required packages, builds the server executable, and creates:

```text
release\Aptitude-Lab-Setup.exe
```

## 3. Install on the lab server PC

Copy `Aptitude-Lab-Setup.exe` to the designated lab server PC and run it as Administrator.

The installer:

- installs Aptitude Lab;
- creates a Desktop shortcut;
- adds a Windows Firewall rule for private LAN traffic on TCP port `8000`;
- opens the local application in the default browser.

Students on the laboratory network access the server at:

```text
http://<SERVER-LAN-IP>:8000
```

## 4. Add future assessment files

After installation, copy every new question-bank pair into:

```text
C:\ProgramData\Aptitude Lab\Question Banks
```

The files must use the same base name:

```text
placement-set-02.html
placement-set-02.json
```

- The HTML file contains the student-facing question content, including tables and inline SVG graphs/diagrams.
- The JSON file contains answer choices, correct answers, categories, difficulty, and explanations.

Open the app, sign in as Faculty, select **Question banks**, click **Refresh folder**, and then click **Import** for the detected pair. Correct answers remain in the server-side SQLite database and are never sent to student browsers.

## Demo accounts

- Student: `1KS23AI042` / `student123`
- Faculty: `faculty` / `faculty123`
