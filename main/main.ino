// Ultrasonic (HC-SR04)
const uint8_t PIN_US_TRIG = 7;
const uint8_t PIN_US_ECHO = 8;

// Pins
const uint8_t PIN_FLEX_THUMB = A0;
const uint8_t PIN_FLEX_INDEX = A1;
const uint8_t PIN_FLEX_MIDDLE = A2;
const uint8_t LED_PIN = 13;

// Calibration data
int flexMin[3] = {1023, 1023, 1023};
int flexMax[3] = {0, 0, 0};

float readDistanceCm()
{
    digitalWrite(PIN_US_TRIG, LOW);
    delayMicroseconds(2);
    digitalWrite(PIN_US_TRIG, HIGH);
    delayMicroseconds(10);
    digitalWrite(PIN_US_TRIG, LOW);

    // Timeout after ~30ms (~5m round trip) so loop doesn't stall.
    unsigned long duration = pulseIn(PIN_US_ECHO, HIGH, 30000UL);
    if (duration == 0)
    {
        return NAN;
    }

    return (duration * 0.0343f) / 2.0f;
}

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

void setup()
{
    pinMode(LED_PIN, OUTPUT);
    digitalWrite(LED_PIN, LOW);
    pinMode(PIN_US_TRIG, OUTPUT);
    pinMode(PIN_US_ECHO, INPUT);
    digitalWrite(PIN_US_TRIG, LOW);

    Serial.begin(115200);
    Serial.println("---AeroMix---");

    calibrateFlexSensors();
}

void loop()
{
    int rawThumb = analogRead(PIN_FLEX_THUMB);
    int rawIndex = analogRead(PIN_FLEX_INDEX);
    int rawMiddle = analogRead(PIN_FLEX_MIDDLE);
    float distanceCm = readDistanceCm();

    Serial.print(rawThumb);
    Serial.print(',');
    Serial.print(rawIndex);
    Serial.print(',');
    Serial.print(rawMiddle);
    Serial.print(',');
    if (isnan(distanceCm))
    {
        Serial.println("nan");
    }
    else
    {
        Serial.println(distanceCm, 1);
    }

    delay(10);
}