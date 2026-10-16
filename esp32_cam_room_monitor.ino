/*
  ESP32-CAM: снимает кадр раз в CAPTURE_INTERVAL_MS и отправляет на сервер
  POST-запросом с JPEG в теле — сервер сам прогонит его через YOLO.

  Плата: AI Thinker ESP32-CAM (Tools -> Board -> AI Thinker ESP32-CAM)
  Библиотеки: встроенные (WiFi.h, esp_camera.h, HTTPClient.h) — ничего
  дополнительно ставить не нужно, если выбрана правильная плата в Boards Manager.

  Прошивка через USB-TTL программатор (FTDI/CP2102):
    GND -> GND, 5V -> 5V, U0R -> TX, U0T -> RX
    Для входа в режим прошивки: соединить GPIO0 с GND перед подачей питания,
    после начала загрузки в Arduino IDE — отсоединить.
*/

#include "esp_camera.h"
#include <WiFi.h>
#include <HTTPClient.h>

// ---- Настройки под себя ----
const char* WIFI_SSID = "твоя_wifi_сеть";
const char* WIFI_PASSWORD = "твой_wifi_пароль";

const char* SERVER_URL = "http://192.168.1.50:8000/api/frame/room_1/upload"; // IP сервера + комната
const unsigned long CAPTURE_INTERVAL_MS = 5000; // как часто снимать и отправлять (мс)

// ---- Распиновка камеры для AI Thinker ESP32-CAM ----
#define PWDN_GPIO_NUM     32
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM      0
#define SIOD_GPIO_NUM     26
#define SIOC_GPIO_NUM     27
#define Y9_GPIO_NUM       35
#define Y8_GPIO_NUM       34
#define Y7_GPIO_NUM       39
#define Y6_GPIO_NUM       36
#define Y5_GPIO_NUM       21
#define Y4_GPIO_NUM       19
#define Y3_GPIO_NUM       18
#define Y2_GPIO_NUM        5
#define VSYNC_GPIO_NUM    25
#define HREF_GPIO_NUM     23
#define PCLK_GPIO_NUM     22

unsigned long lastCaptureTime = 0;

void setup() {
  Serial.begin(115200);
  Serial.println();

  // ---- Инициализация камеры ----
  camera_config_t config;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM;
  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;
  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;
  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;
  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM;
  config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href = HREF_GPIO_NUM;
  config.pin_sscb_sda = SIOD_GPIO_NUM;
  config.pin_sscb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;

  // Разрешение — для детекции людей достаточно VGA/SVGA, не нужен максимум
  if (psramFound()) {
    config.frame_size = FRAMESIZE_SVGA;  // 800x600
    config.jpeg_quality = 12;            // меньше = выше качество (0-63)
    config.fb_count = 2;
  } else {
    config.frame_size = FRAMESIZE_VGA;   // 640x480, если нет PSRAM
    config.jpeg_quality = 15;
    config.fb_count = 1;
  }

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("Ошибка инициализации камеры: 0x%x\n", err);
    return;
  }
  Serial.println("Камера инициализирована");

  // ---- Подключение к WiFi ----
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("Подключение к WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();
  Serial.print("Подключено, IP: ");
  Serial.println(WiFi.localIP());
}

void captureAndSend() {
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) {
    Serial.println("Не удалось получить кадр с камеры");
    return;
  }

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi не подключён, пропускаем отправку");
    esp_camera_fb_return(fb);
    return;
  }

  HTTPClient http;
  http.begin(SERVER_URL);
  http.addHeader("Content-Type", "image/jpeg");

  int httpCode = http.POST(fb->buf, fb->len);

  if (httpCode > 0) {
    Serial.printf("Кадр отправлен (%d байт), ответ сервера: %d\n", fb->len, httpCode);
  } else {
    Serial.printf("Ошибка отправки: %s\n", http.errorToString(httpCode).c_str());
  }

  http.end();
  esp_camera_fb_return(fb);
}

void loop() {
  unsigned long now = millis();
  if (now - lastCaptureTime >= CAPTURE_INTERVAL_MS) {
    lastCaptureTime = now;
    captureAndSend();
  }

  // Переподключение WiFi, если отвалилось
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi отвалился, переподключаемся...");
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    delay(5000);
  }
}
