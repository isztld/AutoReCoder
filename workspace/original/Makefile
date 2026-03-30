CC      = clang
CFLAGS  = -O1 -Wall -Wextra
TARGET  = autorecoder_target

$(TARGET): driver.c miniz.c miniz.h
	$(CC) $(CFLAGS) -o $(TARGET) driver.c miniz.c

clean:
	rm -f $(TARGET) *.o

.PHONY: clean
