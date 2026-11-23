@echo off
rem Open the K09-L design-review website. It is built on demand (it is not shipped, to save disk space):
rem the first time this takes a minute. The app shows the same pages in its Drone mode while a project runs.
set "SITE=%USERPROFILE%\.opencae\k09l_website\index.html"
if not exist "%SITE%" (
  echo Building the K09-L website into %USERPROFILE%\.opencae\k09l_website ...
  python -m opencae.web.build
)
start "" "%SITE%"
