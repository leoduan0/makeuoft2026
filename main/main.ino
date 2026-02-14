#include <Wire.h>
// #include <SoftwareSerial.h>
#include <MPU6050_tockn.h>

// Pins
const uint8_t PIN_FLEX_THUMB = A0;
const uint8_t PIN_FLEX_INDEX = A1;
const uint8_t PIN_FLEX_MIDDLE = A2;
const uint8_t PIN_BT_RX = 2; // HC-05 TXD -> Arduino D2 (RX)
const uint8_t PIN_BT_TX = 3; // HC-05 RXD -> Arduino D3 (TX)
const uint8_t PIN_LED = 13;

// MPU smoothing
const float EMA_ALPHA = 0.2f;
const uint8_t ACC_AVG_SAMPLES = 10;

// SoftwareSerial btSerial(PIN_BT_RX, PIN_BT_TX);
MPU6050 mpu(Wire);

// Calibration data
int flexMin[3] = {1023, 1023, 1023};
int flexMax[3] = {0, 0, 0};

// MPU data
float emaRoll = 0.0f;
float emaPitch = 0.0f;

float accXBuf[ACC_AVG_SAMPLES];
float accYBuf[ACC_AVG_SAMPLES];
float accZBuf[ACC_AVG_SAMPLES];
uint8_t accIndex = 0;
bool accFilled = false;

void calibrateFlexSensors()
{
    unsigned long start = millis();
    while (millis() - start < 5000)
    {
        int v0 = analogRead(PIN_FLEX_THUMB);
        int v1 = analogRead(PIN_FLEX_INDEX);
        int v2 = analogRead(PIN_FLEX_MIDDLE);

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
    mpu.begin();
    delay(200);
    mpu.calcGyroOffsets(true);
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

void setup()
{
    pinMode(PIN_LED, OUTPUT);
    digitalWrite(PIN_LED, LOW);

    Serial.begin(115200);
    // btSerial.begin(9600);

    calibrateFlexSensors();

    Wire.begin();
    initMPU();
}

void loop()
{
    int rawThumb = analogRead(PIN_FLEX_THUMB);
    int rawIndex = analogRead(PIN_FLEX_INDEX);
    int rawMiddle = analogRead(PIN_FLEX_MIDDLE);

    mpu.update();

    float ax = mpu.getAccX();
    float ay = mpu.getAccY();
    float az = mpu.getAccZ();
    updateAccAverages(ax, ay, az);

    float avgAx, avgAy, avgAz;
    getAccAverages(avgAx, avgAy, avgAz);

    float roll = mpu.getAngleX();
    float pitch = mpu.getAngleY();
    float yaw = mpu.getAngleZ();

    emaRoll = EMA_ALPHA * roll + (1.0f - EMA_ALPHA) * emaRoll;
    emaPitch = EMA_ALPHA * pitch + (1.0f - EMA_ALPHA) * emaPitch;

    Serial.print(rawThumb);
    Serial.print(',');
    Serial.print(rawIndex);
    Serial.print(',');
    Serial.print(rawMiddle);
    Serial.print(',');
    Serial.print(emaRoll, 2);
    Serial.print(',');
    Serial.print(emaPitch, 2);
    Serial.print(',');
    Serial.print(yaw, 2);
    Serial.print(',');
    Serial.print(avgAx, 3);
    Serial.print(',');
    Serial.print(avgAy, 3);
    Serial.print(',');
    Serial.println(avgAz, 3);
}