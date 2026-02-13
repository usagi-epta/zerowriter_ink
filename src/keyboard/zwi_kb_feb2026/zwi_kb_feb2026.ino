// ESP32-WROOM Zerowriter Keyboard Driver
// 2025 The Zerowriter Company
// GPL 3.0 LICENSE

//  This program is free software: you can redistribute it and/or modify
//  it under the terms of the GNU General Public License as published by
//  the Free Software Foundation, either version 3 of the License, or
//  (at your option) any later version.

//  This program is distributed in the hope that it will be useful,
//  but WITHOUT ANY WARRANTY; without even the implied warranty of
//  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
//  GNU General Public License for more details.

//  You should have received a copy of the GNU General Public License
//  along with this program.  If not, see <https://www.gnu.org/licenses/>.


#include <Arduino.h>
#include "driver/gpio.h"
#include "esp_wifi.h"
#include "esp_bt.h"
#include "esp_sleep.h"

// Compile for: ESP32-WROOM-DA Module (or similar)
// Note that the ZW keyboard has programming pins at the bottom
// you can program it using a USB-UART adapter or similar,
// or via another MCU. You will need to trigger it in to bootloader
// mode via the boot/reset buttons.
//
// This keyboard firmware works as a standard matrix-scanning
// loop. It utilizes light sleep frequently to operate fairly low
// power without much latency when waking up.
// 
// Each key is sent as a specific byte, which is received and translated by the
// Zerowriter Ink itself. Special keys are sent as modifier signals: shift, 
// ctrl, alt, etc with states for key up and key down. These bytes are what is 
// translated in to keystrokes on the unit via user's keymap.
//
// Architecture improvements we could make:
// - every key could send key down / key up signals instead of just mod keys
// (which would make full-keyboard mapping doable)
// - we should move away from esp32 to a STM chip, or a cheaper/lower power alternative
// - just need something that offers sleep, low power, serial output
//
// - Could also adapt it into a different keyboard firmware standard, but as far as I know,
// there aren't other keyboards that output via serial.

// define keyboard matrix: 5 rows, 14 cols
#define ROWS 5
#define COLS 14

// mod state signals
#define MOD_SHIFT_DOWN 240
#define MOD_SHIFT_UP   241
#define MOD_CTRL_DOWN  242
#define MOD_CTRL_UP    243
#define MOD_ALT_DOWN   244
#define MOD_ALT_UP     245
#define MOD_META_DOWN  246
#define MOD_META_UP    247

// these are used for a "panic" signal that turns off output
// entirely. it could be removed, as there isn't much need for
// it anymore. space_r, space_c means row/col for SPACE key.

constexpr uint8_t SPACE_R = 4, SPACE_C = 6;
constexpr uint8_t LEFT_R  = 4, LEFT_C  = 10;
constexpr uint8_t UP_R    = 4, UP_C    = 11;
constexpr uint8_t RIGHT_R = 4, RIGHT_C = 12;
constexpr uint8_t DOWN_R  = 4, DOWN_C  = 13;

// set up pins. refer to schematic.
const uint8_t rowPins[ROWS] = { 13, 12, 27, 26, 14 };
const uint8_t colPins[COLS] = { 19, 21, 23, 22, 2, 15, 4, 16, 17, 5, 18, 25, 33, 32 };

// timing constants. these change the feel of things.
const unsigned long initialRepeatDelay = 470; // ms before first repeat
const unsigned long repeatTimer       = 75; // ms between subsequent repeats
const unsigned long sleepDelay        = 200; // ms idle before sleeping
const int debounceTime                = 10;  // ms debounce

unsigned long lastKeyPressTime[ROWS][COLS] = { {0} };
bool keyStates[ROWS][COLS]                = { {false} };
bool firstRepeat[ROWS][COLS]              = { {false} };

unsigned long lastInputTime = 0;

// mapping
// we send a byte 0-255 with the value of the key that is 
// inputted, which we de-code on the Zerowriter Ink
// top left value is "`" key, bottom right is RIGHT ARROW key
// 255 value indicates no key in that position on the circuit.

const byte keyIndexMap[ROWS][COLS] = {
  {  0,  1,  2,  3,  4,  5,  6,  7,  8,  9, 10, 11, 12, 13 },
  { 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27 },
  { 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40,255 },
  { 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52,255,255 },
  { 53,255, 54,255, 55,255, 56,255,255,255, 57, 58, 59, 60 }
};

// modifier checks - bool helpers that make the code easier to understand
bool isShift(uint8_t r, uint8_t c)    { byte i = keyIndexMap[r][c]; return i==41 || i==52; }
bool isCtrl(uint8_t r, uint8_t c)     { return keyIndexMap[r][c]==53; }
bool isAlt(uint8_t r, uint8_t c)      { return keyIndexMap[r][c]==54; }
bool isMeta(uint8_t r, uint8_t c)     { return keyIndexMap[r][c]==55; }
bool isCapsLock(uint8_t r, uint8_t c) { return keyIndexMap[r][c]==28; }

bool anyModifierActive() {
  for (uint8_t r=0; r<ROWS; r++)
    for (uint8_t c=0; c<COLS; c++)
      if ( keyStates[r][c] && (isShift(r,c)||isCtrl(r,c)||isAlt(r,c)||isMeta(r,c)) )
        return true;
  return false;
}

// we frequently enter light sleep. light sleep on esp32 can be woken up
// via keypress, without consuming the keypress. very handy.

void enterSleepMode() {
  for (uint8_t i=0; i<ROWS; i++) {
    pinMode(rowPins[i], OUTPUT);
    digitalWrite(rowPins[i], LOW);
  }
  delay(1);
  esp_light_sleep_start();
  for (uint8_t i=0; i<ROWS; i++) {
    pinMode(rowPins[i], INPUT_PULLUP|OUTPUT_OPEN_DRAIN);
    digitalWrite(rowPins[i], HIGH);
  }
  delay(5);
}

// each key is sent using Serial.write, which sends as raw bytes.
// wrapped in to a function so it looks nicer than raw serial writes.
// also could change it to use a different transmission method (like bluetooth)
// or i2c or something.

void sendKeyIndex(byte idx) {
  Serial.write(idx);
}

void handleKeyPress(uint8_t r, uint8_t c) {
  byte idx = keyIndexMap[r][c];
  if (idx==255) return;

  if      (isShift(r,c)) Serial.write(MOD_SHIFT_DOWN);
  else if (isCtrl(r,c))  Serial.write(MOD_CTRL_DOWN);
  else if (isAlt(r,c))   Serial.write(MOD_ALT_DOWN);
  else if (isMeta(r,c))  Serial.write(MOD_META_DOWN);
  else                   sendKeyIndex(idx);

  keyStates[r][c]        = true;
  lastKeyPressTime[r][c] = millis();
  lastInputTime          = millis();
  firstRepeat[r][c]      = false;     // reset first‑repeat flag
  delay(debounceTime);
}

void handleKeyRelease(uint8_t r, uint8_t c) {
  byte idx = keyIndexMap[r][c];
  if (idx==255) return;

  if      (isShift(r,c)) Serial.write(MOD_SHIFT_UP);
  else if (isCtrl(r,c))  Serial.write(MOD_CTRL_UP);
  else if (isAlt(r,c))   Serial.write(MOD_ALT_UP);
  else if (isMeta(r,c))  Serial.write(MOD_META_UP);

  keyStates[r][c]        = false;
  lastKeyPressTime[r][c] = millis();
  lastInputTime          = millis();
  delay(debounceTime);
}

// main processing function. scan through each row/col, send keypress. 
void inputProcessing() {
  for (uint8_t r=0; r<ROWS; r++) {
    digitalWrite(rowPins[r], LOW);
    for (uint8_t c=0; c<COLS; c++) {
      byte idx = keyIndexMap[r][c];
      if (idx==255) continue;

      bool pressed = (digitalRead(colPins[c])==LOW);
      if      (pressed && !keyStates[r][c]) handleKeyPress(r, c);
      else if (!pressed &&  keyStates[r][c]) handleKeyRelease(r, c);

      // — key‑repeat after initial delay —
      if ( keyStates[r][c]
           && !isShift(r,c)
           && !isCtrl(r,c)
           && !isAlt(r,c)
           && !isMeta(r,c)
           && !isCapsLock(r,c) ) {
        unsigned long now = millis();
        if (!firstRepeat[r][c]) {
          if (now - lastKeyPressTime[r][c] >= initialRepeatDelay) {
            sendKeyIndex(idx);
            lastKeyPressTime[r][c] = now;
            lastInputTime          = now;
            firstRepeat[r][c]      = true;
          }
        } else {
          if (now - lastKeyPressTime[r][c] >= repeatTimer) {
            sendKeyIndex(idx);
            lastKeyPressTime[r][c] = now;
            lastInputTime          = now;
          }
        }
      }
    }

    // this is a 5-button keypress to block serial output
    // could be removed, was put in as a safety and for troubleshooting
    if ( keyStates[SPACE_R][SPACE_C]
      && keyStates[LEFT_R ][LEFT_C ]
      && keyStates[UP_R   ][UP_C   ]
      && keyStates[RIGHT_R][RIGHT_C]
      && keyStates[DOWN_R ][DOWN_C ] ) {
      Serial.end();
    }

    digitalWrite(rowPins[r], HIGH);
  }

  // sleep when idle & no modifiers
  // (so don't sleep while someone is holding down Shift, for example)
  if (millis() - lastInputTime > sleepDelay && !anyModifierActive()) {
    enterSleepMode();
  }

  delay(1);
}

// things of note:
// keyboard operates at 921600 baud, which is stable, but many
// USB-UART interfaces don't like a speed that high. if you are wondering
// why you can't see it via an interface, this might be why.
//
// RX serial receiving pin is inactive (-1) as this keyboard only sends signal.
//
// CPU runs at 80Mhz down from 240. It's still far too much processor for a
// simple keyboard matrix, but, the power savings are good.
//
// radios disabled
//

void setup() {
  setCpuFrequencyMhz(80);
  Serial.begin(921600, SERIAL_8N1, -1, 1);
  esp_wifi_deinit();                 
  btStop();                          
  esp_bt_controller_disable();       
  esp_bt_controller_deinit();   
  esp_bt_controller_mem_release(ESP_BT_MODE_CLASSIC_BT);
  esp_bt_controller_mem_release(ESP_BT_MODE_BLE);

  Serial.flush();

  for (uint8_t i=0; i<ROWS; i++) {
    pinMode(rowPins[i], INPUT_PULLUP|OUTPUT_OPEN_DRAIN);
    digitalWrite(rowPins[i], HIGH);
  }
  delay(10);

  for (uint8_t i=0; i<COLS; i++) {
    pinMode(colPins[i], INPUT_PULLUP);
    gpio_wakeup_enable((gpio_num_t)colPins[i], GPIO_INTR_LOW_LEVEL);
  }
  esp_sleep_enable_gpio_wakeup();
}

void loop() {
  while (1) {
  inputProcessing();
  }
}
