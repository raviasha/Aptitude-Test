from pathlib import Path
import hashlib
import re
import runpy

work = Path(__file__).resolve().parent
source = (work / 'build_guide.py').read_text(encoding='utf-8')
release = Path(r'C:\Users\ravis\AppData\Local\Temp\KSATFacultyTimer-d6509f7-20260910\release')
revision = 'd6509f72c93dfa024c09e6d7419e648e8d5a8f1f'
source = source.replace("NAME = 'KSAT_Department_Installation_Guide_2026-09-10'", "NAME = 'KSAT_Department_Installation_Guide_2026-09-10_Timer_Fix'")
source = source.replace('Integrity hardening test build', 'Integrity and faculty timer test build')
source = source.replace('published integrity-hardening test binaries', 'integrity and faculty timer test binaries')
source = source.replace('4c41d30fb4659f8e893969a44a284a274f95f644', revision)
source = source.replace('KSAT source and release files at this guide revision', 'KSAT source at this guide revision')
source = source.replace(
    'Answer several questions, move forward and back, and confirm selections persist. Leave one unanswered to exercise the submission confirmation. Watch the time continue normally. Use the same PC for the whole attempt.',
    'Answer questions and verify saved selections. On Faculty select Extend, enter minutes and a reason, and confirm both student timers and Faculty’s Exam time left increase. Reload Faculty and check min added remains. Different student deadlines appear as a range; an individual extension changes only that student’s deadline. Start window remains separate. Leave one question unanswered and use the same PC throughout.'
)
source = source.replace('Full screen and saved answers', 'Full screen, saved answers and timer extension')
source = source.replace(
    "h('Update both the server and every client')",
    "h('Choose the scope of the update')\np('For this faculty-timer fix, update only the coordinator if clients already have the 10 September integrity update. Skip step 4 and run pilot checks with existing clients. New labs and older installations need both products.')"
)
source = source.replace('Do not leave a mixed client/server rollout in service.', 'Confirm every PC runs an approved compatible build.')
for filename in ('KSATCoordinatorSetup-2.0.0.exe', 'KSATClientSetup-2.0.0.exe', 'KSATCoordinator-2.0.0.exe', 'KSATClient-2.0.0.exe'):
    binary = release / filename
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    pattern = r"p\('" + re.escape(filename) + r"  •  [\d,]+ bytes',True\)\ncode\('[a-f0-9]{64}'\)"
    replacement = f"p('{filename}  •  {binary.stat().st_size:,} bytes',True)\ncode('{digest}')"
    source, count = re.subn(pattern, lambda _: replacement, source)
    assert count == 1, filename
updated = work / 'build_timer_guide.py'
updated.write_text(source, encoding='utf-8')
runpy.run_path(str(updated), run_name='__main__')
