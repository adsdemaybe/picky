@echo off
rem Human-in-the-loop RL for the SO-101: Enter = run, s/f + Enter = label, q = quit.
cd /d "%~dp0..\..\.."
"%~dp0..\..\..\..\.venv-robot\Scripts\python.exe" -m arms.robot.so101.train_loop %*
