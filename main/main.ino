void setup()
{
    pinMode(LED_BUILTIN, OUTPUT);
}

void main()
{
    digitalWrite(LED_BUILTIN, HIGH);
    delay(500);
    digitalWrite(LED_BUILTIN, LOW);
    delay(500);
}