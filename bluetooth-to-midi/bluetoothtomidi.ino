#include <Wire.h>
#include <MPU6050_light.h>
#include <SoftwareSerial.h>

// Pins
const uint8_t LED_PIN = 13;
const uint8_t FLEX_INDEX_PIN = A0;
const uint8_t FLEX_MIDDLE_PIN = A1;
const uint8_t FLEX_RING_PIN = A2;
const uint8_t BT_RX_PIN = 2; // Arduino RX, HC-05 TX
const uint8_t BT_TX_PIN = 3; // Arduino TX, HC-05 RX

// MIDI constants
const uint8_t MIDI_CH = 0;
const uint8_t NOTE_INDEX = 36;
const uint8_t NOTE_MIDDLE = 38;
const uint8_t NOTE_RING = 42;
const uint8_t CC_MOD = 1;
const uint8_t CC_VOL = 7;
const uint8_t CC_ALL_NOTES_OFF = 123;

// Processing constants
const uint32_t CALIBRATION_MS = 5000;
const float PITCH_ROLL_RANGE = 90.0f;
const float PALM_UP_ACC_Z = 0.8f; // g threshold
const uint8_t HYST_HIGH_PCT = 80;
const uint8_t HYST_LOW_PCT = 20;
const uint8_t MPU_AVG_SAMPLES = 10;
const uint32_t MPU_STALE_MS = 500;

SoftwareSerial btSerial(BT_RX_PIN, BT_TX_PIN);
MPU6050 mpu(Wire);

struct FlexCal {
  int minVal;
  int maxVal;
  int highTh;
  int lowTh;
  bool isOn;
};

FlexCal flexIndex{1023, 0, 0, 0, false};
FlexCal flexMiddle{1023, 0, 0, 0, false};
FlexCal flexRing{1023, 0, 0, 0, false};

int lastCcPitch = -1;
int lastCcRoll = -1;
bool safetyLock = false;
unsigned long ledOffAt = 0;
unsigned long lastMpuUpdate = 0;

float pitchBuf[MPU_AVG_SAMPLES];
float rollBuf[MPU_AVG_SAMPLES];
float accZBuf[MPU_AVG_SAMPLES];
uint8_t mpuBufIndex = 0;
bool mpuBufFilled = false;

void blinkLedBrief() {
  digitalWrite(LED_PIN, HIGH);
  ledOffAt = millis() + 5;
}

void sendMidi(uint8_t status, uint8_t data1, uint8_t data2) {
  Serial.write(status | (MIDI_CH & 0x0F));
  Serial.write(data1);
  Serial.write(data2);
  btSerial.write(status | (MIDI_CH & 0x0F));
  btSerial.write(data1);
  btSerial.write(data2);
  blinkLedBrief();
}

int mapToMidi(int value, int minVal, int maxVal) {
  if (maxVal <= minVal) {
    return 0;
  }
  long mapped = map(constrain(value, minVal, maxVal), minVal, maxVal, 0, 127);
  return (int)constrain(mapped, 0, 127);
}

void updateThresholds(FlexCal &cal) {
  int range = cal.maxVal - cal.minVal;
  if (range < 10) {
    range = 10;
  }
  cal.highTh = cal.minVal + (range * HYST_HIGH_PCT) / 100;
  cal.lowTh = cal.minVal + (range * HYST_LOW_PCT) / 100;
}

void calibrateFlexSensors() {
  unsigned long start = millis();
  while (millis() - start < CALIBRATION_MS) {
    int vIndex = analogRead(FLEX_INDEX_PIN);
    int vMiddle = analogRead(FLEX_MIDDLE_PIN);
    int vRing = analogRead(FLEX_RING_PIN);

    flexIndex.minVal = min(flexIndex.minVal, vIndex);
    flexIndex.maxVal = max(flexIndex.maxVal, vIndex);
    flexMiddle.minVal = min(flexMiddle.minVal, vMiddle);
    flexMiddle.maxVal = max(flexMiddle.maxVal, vMiddle);
    flexRing.minVal = min(flexRing.minVal, vRing);
    flexRing.maxVal = max(flexRing.maxVal, vRing);

    delay(5);
  }

  updateThresholds(flexIndex);
  updateThresholds(flexMiddle);
  updateThresholds(flexRing);
}

void scanI2C() {
  Serial.println("I2C scan start");
  bool foundMpu = false;
  for (uint8_t addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      Serial.print("Found 0x");
      if (addr < 16) {
        Serial.print("0");
      }
      Serial.println(addr, HEX);
      if (addr == 0x68) {
        foundMpu = true;
      }
    }
  }
  if (foundMpu) {
    Serial.println("MPU6050 detected at 0x68");
  } else {
    Serial.println("MPU6050 not detected at 0x68");
  }
  Serial.println("I2C scan end");
}

bool reinitMpu() {
  Wire.end();
  delay(10);
  Wire.begin();
  byte status = mpu.begin();
  if (status == 0) {
    mpu.calcOffsets();
    lastMpuUpdate = millis();
    return true;
  }
  return false;
}

void updateMpuAverages(float pitch, float roll, float accZ) {
  pitchBuf[mpuBufIndex] = pitch;
  rollBuf[mpuBufIndex] = roll;
  accZBuf[mpuBufIndex] = accZ;
  mpuBufIndex = (mpuBufIndex + 1) % MPU_AVG_SAMPLES;
  if (mpuBufIndex == 0) {
    mpuBufFilled = true;
  }
}

void getMpuAverages(float &pitch, float &roll, float &accZ) {
  uint8_t count = mpuBufFilled ? MPU_AVG_SAMPLES : mpuBufIndex;
  if (count == 0) {
    pitch = roll = accZ = 0.0f;
    return;
  }
  float sumPitch = 0.0f;
  float sumRoll = 0.0f;
  float sumAccZ = 0.0f;
  for (uint8_t i = 0; i < count; i++) {
    sumPitch += pitchBuf[i];
    sumRoll += rollBuf[i];
    sumAccZ += accZBuf[i];
  }
  pitch = sumPitch / count;
  roll = sumRoll / count;
  accZ = sumAccZ / count;
}

void setup() {
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  Serial.begin(115200);
  btSerial.begin(115200);
  Wire.begin();

  scanI2C();

  byte mpuStatus = mpu.begin();
  if (mpuStatus == 0) {
    mpu.calcOffsets();
    lastMpuUpdate = millis();
  }

  calibrateFlexSensors();
}

void handleFlex(FlexCal &cal, int pin, uint8_t note) {
  int value = analogRead(pin);
  int velocity = mapToMidi(value, cal.minVal, cal.maxVal);

  if (!cal.isOn && value >= cal.highTh) {
    sendMidi(0x90, note, velocity);
    cal.isOn = true;
  } else if (cal.isOn && value <= cal.lowTh) {
    sendMidi(0x80, note, 0);
    cal.isOn = false;
  }
}

void handleMpu() {
  mpu.update();
  lastMpuUpdate = millis();
  float pitch = mpu.getAngleY();
  float roll = mpu.getAngleX();
  float accZ = mpu.getAccZ();
  updateMpuAverages(pitch, roll, accZ);

  float avgPitch = 0.0f;
  float avgRoll = 0.0f;
  float avgAccZ = 0.0f;
  getMpuAverages(avgPitch, avgRoll, avgAccZ);

  int ccPitch = mapToMidi((int)constrain(avgPitch, -PITCH_ROLL_RANGE, PITCH_ROLL_RANGE),
                          (int)-PITCH_ROLL_RANGE, (int)PITCH_ROLL_RANGE);
  int ccRoll = mapToMidi((int)constrain(avgRoll, -PITCH_ROLL_RANGE, PITCH_ROLL_RANGE),
                         (int)-PITCH_ROLL_RANGE, (int)PITCH_ROLL_RANGE);

  if (ccPitch != lastCcPitch) {
    sendMidi(0xB0, CC_MOD, (uint8_t)ccPitch);
    lastCcPitch = ccPitch;
  }

  if (ccRoll != lastCcRoll) {
    sendMidi(0xB0, CC_VOL, (uint8_t)ccRoll);
    lastCcRoll = ccRoll;
  }

  bool palmUp = avgAccZ > PALM_UP_ACC_Z;
  if (palmUp && !safetyLock) {
    sendMidi(0xB0, CC_ALL_NOTES_OFF, 0);
    sendMidi(0xB0, CC_MOD, 0);
    sendMidi(0xB0, CC_VOL, 0);
    safetyLock = true;
  } else if (!palmUp && safetyLock) {
    safetyLock = false;
  }
}

void loop() {
  if (ledOffAt != 0 && millis() >= ledOffAt) {
    digitalWrite(LED_PIN, LOW);
    ledOffAt = 0;
  }

  handleMpu();
  handleFlex(flexIndex, FLEX_INDEX_PIN, NOTE_INDEX);
  handleFlex(flexMiddle, FLEX_MIDDLE_PIN, NOTE_MIDDLE);
  handleFlex(flexRing, FLEX_RING_PIN, NOTE_RING);

  if (millis() - lastMpuUpdate > MPU_STALE_MS) {
    reinitMpu();
  }
}
