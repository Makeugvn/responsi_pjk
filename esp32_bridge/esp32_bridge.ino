
#include <WiFi.h>
#include <HTTPClient.h>
#include <string.h>

// ==========================================
// WIFI CONFIGURATION
// ==========================================

const char* WIFI_SSID = "UGM-Hotspot";
const char* WIFI_PASSWORD = "";

// Ganti dengan IP laptop yang menjalankan receiver.py
const char* BATCH_URL =
    "http://10.38.22.24:5000/batch";


// ==========================================
// UART CONFIGURATION
// ==========================================

#define BAUD_RATE 115200

#define FRAME_SIZE 32

// Perbesar buffer RX agar bisa menampung
// banyak frame sekaligus dari transmitter
#define UART_RX_BUFFER 8192

#define HTTP_RETRIES 3

#define HTTP_TIMEOUT 10000


// ==========================================
// BATCH CONFIGURATION
// ==========================================

// Maksimal frame yang dikumpulkan sebelum
// dikirim dalam satu HTTP POST
#define BATCH_MAX_FRAMES 64

// Buffer untuk menampung batch frame
#define BATCH_BUFFER_SIZE (BATCH_MAX_FRAMES * FRAME_SIZE)

// Waktu tunggu (ms) setelah frame terakhir
// sebelum mengirim batch yang belum penuh
#define BATCH_COLLECT_MS 15


// ==========================================
// GLOBAL BUFFERS
// ==========================================

uint8_t batchBuffer[BATCH_BUFFER_SIZE];
uint16_t batchSequences[BATCH_MAX_FRAMES];

// Persistent WiFiClient untuk HTTP keep-alive
WiFiClient persistentClient;


// ==========================================
// SEQUENCE NUMBER
// ==========================================

uint16_t getSequence(const uint8_t* frame) {

    return (
        (uint16_t)frame[0]
        |
        ((uint16_t)frame[1] << 8)
    );
}


// ==========================================
// SEND ACK (tanpa flush per-panggilan)
// ==========================================

void sendACK(uint16_t sequence) {

    Serial.printf(
        "ACK,%u\n",
        (unsigned int)sequence
    );
}


// ==========================================
// SEND NACK (tanpa flush per-panggilan)
// ==========================================

void sendNACK(
    uint16_t sequence,
    int status
) {

    Serial.printf(
        "NACK,%u,%d\n",
        (unsigned int)sequence,
        status
    );
}


// ==========================================
// WIFI CONNECTION
// ==========================================

bool connectWiFi() {

    if (WiFi.status() == WL_CONNECTED) {
        return true;
    }

    WiFi.mode(WIFI_STA);

    WiFi.begin(
        WIFI_SSID,
        WIFI_PASSWORD
    );

    Serial.println("WIFI_CONNECTING");

    unsigned long start = millis();

    while (
        WiFi.status() != WL_CONNECTED
        &&
        millis() - start < 15000
    ) {

        delay(300);
    }

    if (WiFi.status() != WL_CONNECTED) {
        return false;
    }

    return true;
}


// ==========================================
// HTTP POST BATCH
// ==========================================

bool sendBatchHTTP(
    int frameCount,
    int &finalStatus
) {

    finalStatus = -1;

    int totalBytes = frameCount * FRAME_SIZE;

    for (
        int attempt = 0;
        attempt < HTTP_RETRIES;
        attempt++
    ) {

        if (!connectWiFi()) {

            finalStatus = -3;
            continue;
        }

        HTTPClient http;

        http.setReuse(true);
        http.setTimeout(HTTP_TIMEOUT);

        if (!http.begin(
            persistentClient,
            BATCH_URL
        )) {

            finalStatus = -2;
            continue;
        }

        http.addHeader(
            "Content-Type",
            "application/octet-stream"
        );

        finalStatus = http.POST(
            batchBuffer,
            totalBytes
        );

        http.end();

        // HTTP 200-299 = berhasil
        if (
            finalStatus >= 200
            &&
            finalStatus < 300
        ) {

            return true;
        }

        // Reset koneksi jika gagal
        persistentClient.stop();
    }

    return false;
}


// ==========================================
// SETUP
// ==========================================

void setup() {

    // Perbesar buffer RX sebelum Serial.begin()
    Serial.setRxBufferSize(
        UART_RX_BUFFER
    );

    Serial.begin(
        BAUD_RATE
    );

    // Timeout pendek untuk readBytes
    Serial.setTimeout(100);

    delay(300);

    // Sambungkan ke WiFi sebelum transmitter berjalan
    if (!connectWiFi()) {

        Serial.println("WIFI_FAILED");
    }

    // Penanda bahwa ESP32 siap menerima frame
    if (WiFi.status() == WL_CONNECTED) {

        Serial.println("READY");
    }
}


// ==========================================
// MAIN LOOP
// ==========================================

void loop() {

    // Tunggu sampai tersedia satu frame penuh
    if (
        Serial.available()
        < FRAME_SIZE
    ) {

        // Jika WiFi belum tersambung, coba sambungkan kembali
        if (WiFi.status() != WL_CONNECTED) {
            connectWiFi();
        }

        return;
    }

    // ======================================
    // KUMPULKAN FRAME KE DALAM BATCH
    // ======================================

    int frameCount = 0;
    unsigned long lastDataTime = millis();

    while (frameCount < BATCH_MAX_FRAMES) {

        if (Serial.available() >= FRAME_SIZE) {

            size_t bytesRead = Serial.readBytes(
                batchBuffer + (frameCount * FRAME_SIZE),
                FRAME_SIZE
            );

            if (bytesRead == FRAME_SIZE) {

                batchSequences[frameCount] =
                    getSequence(
                        batchBuffer
                        + (frameCount * FRAME_SIZE)
                    );

                frameCount++;
                lastDataTime = millis();
            }

        } else if (
            millis() - lastDataTime
            >= BATCH_COLLECT_MS
        ) {

            // Tidak ada data baru, kirim
            // batch yang sudah terkumpul
            break;

        } else {

            // Beri waktu task lain berjalan
            yield();
        }
    }

    if (frameCount == 0) {
        return;
    }

    // ======================================
    // KIRIM BATCH VIA HTTP
    // ======================================

    int httpStatus = -1;

    bool success = sendBatchHTTP(
        frameCount,
        httpStatus
    );

    // ======================================
    // KIRIM ACK/NACK UNTUK SETIAP FRAME
    // ======================================

    for (int i = 0; i < frameCount; i++) {

        if (success) {

            sendACK(batchSequences[i]);

        } else {

            sendNACK(
                batchSequences[i],
                httpStatus
            );
        }
    }

    // Flush sekali setelah semua ACK terkirim
    Serial.flush();
}
