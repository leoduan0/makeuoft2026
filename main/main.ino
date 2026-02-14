#include <Wire.h>
#include <SoftwareSerial.h>
#include <MPU6050_light.h>

// Pins
const uint8_t PIN_FLEX_INDEX = A0;
const uint8_t PIN_FLEX_MIDDLE = A1;
const uint8_t PIN_FLEX_RING = A2;
const uint8_t PIN_BT_RX = 2; // HC-05 TX -> Arduino RX
const uint8_t PIN_BT_TX = 3; // HC-05 RX -> Arduino TX
const uint8_t PIN_LED = 13;

// MIDI
const uint8_t MIDI_CH = 0;      // channel 1
const uint8_t NOTE_INDEX = 36;  // Kick
const uint8_t NOTE_MIDDLE = 38; // Snare
const uint8_t NOTE_RING = 42;   // Hi-Hat
const uint8_t CC_VOLUME = 7;
const uint8_t CC_MOD = 1;

// Flex hysteresis (scaled 0-127)
const uint8_t FLEX_HIGH = 102; // 80%
const uint8_t FLEX_LOW = 25;   // 20%

// MPU smoothing
const float EMA_ALPHA = 0.2f;
const uint8_t ACC_AVG_SAMPLES = 10;

SoftwareSerial btSerial(PIN_BT_RX, PIN_BT_TX);
MPU6050 mpu(Wire);

// Calibration data
int flexMin[3] = {1023, 1023, 1023};
int flexMax[3] = {0, 0, 0};

// Flex state
bool flexPressed[3] = {false, false, false};

// MPU data
float emaRoll = 0.0f;
float emaPitch = 0.0f;

float accXBuf[ACC_AVG_SAMPLES];
float accYBuf[ACC_AVG_SAMPLES];
float accZBuf[ACC_AVG_SAMPLES];
uint8_t accIndex = 0;
bool accFilled = false;

unsigned long lastMpuUpdate = 0;
bool mpuReady = false;

void pulseLed()
{
    digitalWrite(PIN_LED, HIGH);
    delayMicroseconds(200);
    digitalWrite(PIN_LED, LOW);
}

void sendMIDI(uint8_t status, uint8_t data1, uint8_t data2)
{
    btSerial.write(status | (MIDI_CH & 0x0F));
    btSerial.write(data1);
    btSerial.write(data2);
    pulseLed();
}

void sendNoteOn(uint8_t note, uint8_t velocity)
{
    sendMIDI(0x90, note, velocity);
}

void sendNoteOff(uint8_t note)
{
    sendMIDI(0x80, note, 0);
}

void sendCC(uint8_t cc, uint8_t value)
{
    sendMIDI(0xB0, cc, value);
}

float mapFloat(float x, float inMin, float inMax, float outMin, float outMax)
{
    if (inMax - inMin == 0)
        return outMin;
    return (x - inMin) * (outMax - outMin) / (inMax - inMin) + outMin;
}

uint8_t mapFlexToMidi(int raw, int minVal, int maxVal)
{
    if (maxVal - minVal < 5)
        return 0;
    int clipped = constrain(raw, minVal, maxVal);
    int mapped = (int)mapFloat((float)clipped, (float)minVal, (float)maxVal, 0.0f, 127.0f);
    return (uint8_t)constrain(mapped, 0, 127);
}

void calibrateFlexSensors()
{
    unsigned long start = millis();
    while (millis() - start < 5000)
    {
        int v0 = analogRead(PIN_FLEX_INDEX);
        int v1 = analogRead(PIN_FLEX_MIDDLE);
        int v2 = analogRead(PIN_FLEX_RING);

        flexMin[0] = min(flexMin[0], v0);
        flexMin[1] = min(flexMin[1], v1);
        flexMin[2] = min(flexMin[2], v2);

        flexMax[0] = max(flexMax[0], v0);
        flexMax[1] = max(flexMax[1], v1);
        flexMax[2] = max(flexMax[2], v2);

        delay(10);
    }

    for (uint8_t i = 0; i < 3; i++)
    {
        if (flexMax[i] - flexMin[i] < 10)
        {
            flexMax[i] = flexMin[i] + 10;
        }
    }
}

bool initMPU()
{
    Wire.begin();
    byte status = mpu.begin();
    if (status != 0)
    {
        return false;
    }
    delay(200);
    mpu.calcOffsets(true, true);
    lastMpuUpdate = millis();
    return true;
}

void updateAccAverages(float ax, float ay, float az)
{
    accXBuf[accIndex] = ax;
    accYBuf[accIndex] = ay;
    accZBuf[accIndex] = az;
    accIndex = (accIndex + 1) % ACC_AVG_SAMPLES;
    if (accIndex == 0)
        accFilled = true;
}

void getAccAverages(float &ax, float &ay, float &az)
{
    uint8_t count = accFilled ? ACC_AVG_SAMPLES : accIndex;
    if (count == 0)
    {
        ax = ay = az = 0;
        return;
    }
    float sumX = 0, sumY = 0, sumZ = 0;
    for (uint8_t i = 0; i < count; i++)
    {
        sumX += accXBuf[i];
        sumY += accYBuf[i];
        sumZ += accZBuf[i];
    }
    ax = sumX / count;
    ay = sumY / count;
    az = sumZ / count;
}

void safetyLock()
{
    sendCC(CC_VOLUME, 0);
    sendCC(CC_MOD, 0);
    sendCC(123, 0); // All Notes Off
    flexPressed[0] = flexPressed[1] = flexPressed[2] = false;
}

void setup()
{
    pinMode(PIN_LED, OUTPUT);
    digitalWrite(PIN_LED, LOW);

    Serial.begin(115200);
    btSerial.begin(115200);

    Serial.println("Aero-Mix DJ Glove boot...");
    Serial.print("MPU6050 I2C address: 0x");
    Serial.println(0x68, HEX);

    calibrateFlexSensors();

    mpuReady = initMPU();
}

void loop()
{
    // Flex processing
    int rawIndex = analogRead(PIN_FLEX_INDEX);
    int rawMiddle = analogRead(PIN_FLEX_MIDDLE);
    int rawRing = analogRead(PIN_FLEX_RING);

    uint8_t flexIndexVal = mapFlexToMidi(rawIndex, flexMin[0], flexMax[0]);
    uint8_t flexMiddleVal = mapFlexToMidi(rawMiddle, flexMin[1], flexMax[1]);
    uint8_t flexRingVal = mapFlexToMidi(rawRing, flexMin[2], flexMax[2]);

    // Index finger
    if (!flexPressed[0] && flexIndexVal >= FLEX_HIGH)
    {
        sendNoteOn(NOTE_INDEX, flexIndexVal);
        flexPressed[0] = true;
    }
    else if (flexPressed[0] && flexIndexVal <= FLEX_LOW)
    {
        sendNoteOff(NOTE_INDEX);
        flexPressed[0] = false;
    }

    // Middle finger
    if (!flexPressed[1] && flexMiddleVal >= FLEX_HIGH)
    {
        sendNoteOn(NOTE_MIDDLE, flexMiddleVal);
        flexPressed[1] = true;
    }
    else if (flexPressed[1] && flexMiddleVal <= FLEX_LOW)
    {
        sendNoteOff(NOTE_MIDDLE);
        flexPressed[1] = false;
    }

    // Ring finger
    if (!flexPressed[2] && flexRingVal >= FLEX_HIGH)
    {
        sendNoteOn(NOTE_RING, flexRingVal);
        flexPressed[2] = true;
    }
    else if (flexPressed[2] && flexRingVal <= FLEX_LOW)
    {
        sendNoteOff(NOTE_RING);
        flexPressed[2] = false;
    }

    // MPU processing with watchdog-style recovery
    if (!mpuReady)
    {
        mpuReady = initMPU();
        delay(50);
        return;
    }

    mpu.update();

    lastMpuUpdate = millis();

    float ax = mpu.getAccX();
    float ay = mpu.getAccY();
    float az = mpu.getAccZ();
    updateAccAverages(ax, ay, az);

    float avgAx, avgAy, avgAz;
    getAccAverages(avgAx, avgAy, avgAz);

    // Safety lock: palm facing ceiling (upside down)
    if (avgAz < -0.5f)
    {
        safetyLock();
    }

    float roll = mpu.getAngleX();
    float pitch = mpu.getAngleY();

    emaRoll = EMA_ALPHA * roll + (1.0f - EMA_ALPHA) * emaRoll;
    emaPitch = EMA_ALPHA * pitch + (1.0f - EMA_ALPHA) * emaPitch;

    int ccVol = (int)mapFloat(constrain(emaRoll, -90.0f, 90.0f), -90.0f, 90.0f, 0.0f, 127.0f);
    int ccMod = (int)mapFloat(constrain(emaPitch, -90.0f, 90.0f), -90.0f, 90.0f, 0.0f, 127.0f);

    sendCC(CC_VOLUME, (uint8_t)constrain(ccVol, 0, 127));
    sendCC(CC_MOD, (uint8_t)constrain(ccMod, 0, 127));

    if (millis() - lastMpuUpdate > 500)
    {
        mpuReady = false;
    }
}