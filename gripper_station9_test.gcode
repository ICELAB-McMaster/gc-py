; gripper_station9_test: gripper pick at gripper_pick_9, 1 cycle(s)
; XY (69.5, 178.2); approach Z 90, grip Z 107, release Z 100 (+Z is DOWN)
; grip at 500 mA, hold 1 s at Z 90
; WARNING the release is 7 mm ABOVE the grip, so the part FALLS 7 mm. Declared deliberately.
; IMU -> ONE csv for the whole run, ~/gripper_captures/gripper_station9_test_<stamp>.csv, at 50 Hz on both sensors. Long format: every row has source=PCB (board IMU, over CAN), source=plate (the Pi's I2C IMU on the plate carrier) or source=mark (a phase label). There is no separate marks file
; both IMUs, spanning the GANTRY motion only - the grip and release sit outside the capture and their transients come from their own --log CSVs. --imu-mode spanning trades the board IMU for a capture that covers them
; IMU start/stop needs host/imu_session.py, which is NEW and has never been run on hardware
; PREFLIGHT - if any line below reports 'Unknown command', STOP: gripper_macros.cfg is not included from printer.cfg and nothing in this file will actually grip. If the version printed is older than you expect, the INSTALLED copy has drifted from the repo. See docs/LAB.md.
M400 
GRIPPER_MACROS_VERSION
G4 P1500
M400 
GRIPPER_PING
G4 P1500
M400 
GRIPPER_STATUS
G4 P1500
G28 Z
G28 X Y
G0 F6000 Z5
M400 
M400 
GRIPPER_IMU_RUN_BEGIN NAME=gripper_station9_test
G4 P1500
G0 F6000 X69.5 Y0 Z5
G0 F6000 X69.5 Y178.2 Z5
; --- pick 1 of 1 ---
G0 F6000 Z90
M400 
GRIPPER_HOME
G4 P1500
M400 
GRIPPER_OPEN
G4 P1500
G0 F6000 Z107
M400 
GRIPPER_GRIP MA=500 LOG=~/gripper_captures/gripper_station9_test_01_grip.csv
G4 P2000
M400 
GRIPPER_IMU_START LABEL=gripper_station9_test_01 RATE=50
G4 P1500
M400 
GRIPPER_IMU_MARK LABEL=pick01_gripped_500ma_z107
G4 P1500
G0 F6000 Z90
M400 
GRIPPER_IMU_MARK LABEL=pick01_lifted_z90
G4 P1500
G4 P1000
M400 
GRIPPER_IMU_MARK LABEL=pick01_held_1s
G4 P1500
G0 F6000 Z100
M400 
GRIPPER_IMU_MARK LABEL=pick01_at_release_z100
G4 P1500
M400 
GRIPPER_IMU_STOP
G4 P1500
M400 
GRIPPER_RELEASE LOG=~/gripper_captures/gripper_station9_test_01_release.csv
G4 P1500
G4 P1000
G0 F6000 Z90
M400 
GRIPPER_IMU_RUN_END
G4 P1500
G0 F6000 Z5
G0 F6000 X420 Y178.2 Z5
G0 F6000 X420 Y0 Z5
G0 F6000 Z0
M400 
