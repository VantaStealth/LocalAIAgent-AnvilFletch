@echo off
title Anvil Fletch
cd /d "%~dp0"
set OLLAMA_IGPU_ENABLE=1
if not exist bin mkdir bin

set GXX=C:\msys64\ucrt64\bin\g++.exe
set LDFLAGS=-static -static-libgcc -static-libstdc++ -lcomctl32 -ldwmapi -luxtheme -lgdiplus -lole32 -luuid -lgdi32 -luser32 -lshell32

if not exist bin\AnvilFletch.exe (
    echo Compiling Anvil Fletch...
    "%GXX%" -O2 -std=c++17 -mwindows -municode src\main.cpp -o bin\AnvilFletch.exe %LDFLAGS%
)
if exist bin\AnvilFletch.exe (
    start "" bin\AnvilFletch.exe
) else (
    echo Build failed. Run: make
)
