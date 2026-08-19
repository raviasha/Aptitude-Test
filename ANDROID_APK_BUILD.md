# Aptitude Mobile APK

The Android project lives under `android/` and is isolated on the mobile branch.
It packages the existing FastAPI test engine with Chaquopy and displays the
existing question/test renderer inside a locked-down Android WebView.

## Configure the Google Drive repository

The Drive folder can be configured in either place:

1. Before building, copy `android/mobile-config.properties.example` to
   `android/mobile-config.properties` and set `drive.folderUrl` to the shared
   folder link or folder ID. The real file is ignored by Git.
2. In the installed APK, open **Repository settings** and enter or replace the
   folder link. The runtime value is stored in app-private local storage and
   takes precedence for that device.

The designated folder should contain version 2 question-bank ZIP packages.
Question-bank ZIPs are downloaded at runtime and are never packaged in the APK.

## Google authorization prerequisite

Before Drive authorization can work on a device:

- Enable the Google Drive API in a Google Cloud project.
- Create an Android OAuth client for package `com.aptitudelab.mobile.debug`
  when testing the debug APK. Register the SHA-1 fingerprint of the debug
  signing certificate used by the build machine.
- For a production-signed release, use package `com.aptitudelab.mobile` and
  register the SHA-1 fingerprint of the production signing certificate.
- Configure the OAuth consent screen and grant the test users access while the
  app remains in testing status.

The app requests read-only Drive access only when the student opens the remote
catalogue. It does not request, receive, or store a Google password. Access
tokens are held in memory and are not written to the local results database.

## Build

The build needs JDK 17, Android SDK platform/build tools 35, a Gradle wrapper,
and a host Python 3.12 interpreter for Chaquopy bytecode and package tasks.

```powershell
.\android\build-android.ps1 `
  -DriveFolderUrl "https://drive.google.com/drive/folders/YOUR_FOLDER_ID" `
  -AndroidSdk "C:\path\to\android-sdk" `
  -JavaHomePath "C:\path\to\jdk-17" `
  -PythonExecutable "C:\path\to\python.exe"
```

The installable debug APK is copied to
`release/Aptitude-Lab-Mobile-debug.apk`. A production release additionally
requires an owner-controlled signing keystore and the matching OAuth client.

## Mobile behavior

- No demo accounts, starter questions, templates, or question-bank ZIPs are
  included in the generated Android Python bundle.
- Permanent downloads are checked against available device storage before the
  transfer starts and are registered only after a complete, valid v2 package
  has been parsed.
- **Use now** imports a temporary cache compatible with the existing loader;
  it is cleaned on a later app start after active practice has finished.
- Deleting a bank snapshots completed result details before removing the bank's
  questions and assets, so local practice history remains available offline.
- The Drive provider is behind a small content-provider interface; a future
  REST provider can replace it without changing the test/scoring engine.
