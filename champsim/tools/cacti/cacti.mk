TARGET = cacti
SHELL = /bin/sh
.PHONY: all depend clean
.SUFFIXES: .cc .o

ifndef NTHREADS
  NTHREADS = 8
endif


LIBS = 
INCS = -lm

# Vendored change. Upstream assumes an x86 GCC: -gstabs+ was dropped from GCC and is
# rejected by clang, and -msse2/-mfpmath=sse do not exist off x86. Pick them by machine so
# one tree builds on x86_64 Linux and on Apple silicon.
ARCH := $(shell uname -m)
ifneq ($(filter x86_64 amd64 i386 i686,$(ARCH)),)
  ARCHFLAGS = -msse2 -mfpmath=sse
else
  ARCHFLAGS =
endif

ifeq ($(TAG),dbg)
  DBG = -Wall
  OPT = -ggdb -g -O0 -DNTHREADS=1
else
  DBG =
  OPT = -g $(ARCHFLAGS) -DNTHREADS=$(NTHREADS)
endif

# Vendored change. This code predates C++11, which gave a name written directly after a
# string literal a new meaning, so a current compiler rejects lines CACTI has always had.
# Build it as the language it was written in instead of editing those lines.
STD = -std=gnu++98

#CXXFLAGS = -Wall -Wno-unknown-pragmas -Winline $(DBG) $(OPT)
CXXFLAGS = -Wno-unknown-pragmas $(STD) $(DBG) $(OPT)
CXX = g++
CC  = gcc

SRCS  = area.cc bank.cc mat.cc main.cc Ucache.cc io.cc technology.cc basic_circuit.cc parameter.cc \
		decoder.cc component.cc uca.cc subarray.cc wire.cc htree2.cc extio.cc extio_technology.cc \
		cacti_interface.cc router.cc nuca.cc crossbar.cc arbiter.cc powergating.cc TSV.cc memorybus.cc \
		memcad.cc memcad_parameters.cc
		

OBJS = $(patsubst %.cc,obj_$(TAG)/%.o,$(SRCS))
PYTHONLIB_SRCS = $(patsubst main.cc, ,$(SRCS)) obj_$(TAG)/cacti_wrap.cc
PYTHONLIB_OBJS = $(patsubst %.cc,%.o,$(PYTHONLIB_SRCS)) 
INCLUDES       = -I /usr/include/python2.4 -I /usr/lib/python2.4/config

all: obj_$(TAG)/$(TARGET)
	cp -f obj_$(TAG)/$(TARGET) $(TARGET)

obj_$(TAG)/$(TARGET) : $(OBJS)
	$(CXX) $(OBJS) -o $@ $(INCS) $(CXXFLAGS) $(LIBS) -pthread

#obj_$(TAG)/%.o : %.cc
#	$(CXX) -c $(CXXFLAGS) $(INCS) -o $@ $<

obj_$(TAG)/%.o : %.cc
	$(CXX) $(CXXFLAGS) -c $< -o $@

clean:
	-rm -f *.o _cacti.so cacti.py $(TARGET)


