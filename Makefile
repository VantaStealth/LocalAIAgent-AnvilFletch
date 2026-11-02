# Anvil Fletch - C++ Build System
# Compiler: MinGW-w64 UCRT GCC (g++)
#
# NOTE on -static: without it the exe binds to whichever libstdc++-6.dll is
# first on PATH. Git for Windows ships an incompatible one, which crashes the
# app at startup. Static linking removes the runtime dependency entirely.

CXX      = C:\msys64\ucrt64\bin\g++.exe
CXXFLAGS = -O2 -std=c++17 -mwindows -municode -Wall -Wno-unused-variable -Wno-unused-function
LDFLAGS  = -static -static-libgcc -static-libstdc++ \
           -lcomctl32 -ldwmapi -luxtheme -lgdiplus -lole32 -luuid -lgdi32 -luser32 -lshell32

TARGET = bin\AnvilFletch.exe
SRC    = src\main.cpp

all: $(TARGET)

$(TARGET): $(SRC)
	@if not exist bin mkdir bin
	$(CXX) $(CXXFLAGS) $(SRC) -o $(TARGET) $(LDFLAGS)
	@echo [OK] Built AnvilFletch.exe

clean:
	@if exist $(TARGET) del $(TARGET)
