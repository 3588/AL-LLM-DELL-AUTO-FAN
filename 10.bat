@echo off
REM 启用手动风扇控制
ipmitool -I lanplus -H 10.10.10.194 -U root -P calvin raw 0x30 0x30 0x01 0x00
REM 设置风扇速度为 10%
ipmitool -I lanplus -H 10.10.10.194 -U root -P calvin raw 0x30 0x30 0x02 0xff 0x0A
pause
