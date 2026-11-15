/*
 * WAV input and output for AtracDEnc without libsndfile, for XLT Tool's AppImage
 * (packaging/build-appimage.sh puts it in place of src/pcm_io_sndfile.cpp and links
 * the program fully static).
 * 16-bit PCM WAV only, which is what XLT Tool gives it (CD audio tracks).
 *
 * This file is part of the AtracDEnc build in XLT Tool and, like AtracDEnc, is free
 * software; you can redistribute it and/or modify it under the terms of the GNU
 * Lesser General Public License as published by the Free Software Foundation;
 * either version 2.1 of the License, or (at your option) any later version.
 */

#include <cstdint>  /* before wav.h: libsndfile's header used to bring these in */
#include "wav.h"

#include <cstdio>
#include <cstring>
#include <stdexcept>
#include <string>
#include <vector>

class TPCMIOWav : public IPCMProviderImpl {
public:
    explicit TPCMIOWav(const std::string& filename) {
        F = std::fopen(filename.c_str(), "rb");
        if (!F)
            throw std::runtime_error("can't open " + filename);
        unsigned char h[12];
        if (std::fread(h, 1, 12, F) != 12 || std::memcmp(h, "RIFF", 4) || std::memcmp(h + 8, "WAVE", 4))
            throw std::runtime_error("not a WAV file: " + filename);
        bool fmt = false;
        for (;;) {
            unsigned char c[8];
            if (std::fread(c, 1, 8, F) != 8)
                throw std::runtime_error("no data in " + filename);
            uint32_t len = c[4] | c[5] << 8 | c[6] << 16 | (uint32_t)c[7] << 24;
            if (!std::memcmp(c, "fmt ", 4)) {
                std::vector<unsigned char> b(len);
                if (len < 16 || std::fread(b.data(), 1, len, F) != len)
                    throw std::runtime_error("bad WAV header: " + filename);
                if ((b[0] | b[1] << 8) != 1 || (b[14] | b[15] << 8) != 16)
                    throw std::runtime_error("only 16-bit PCM WAV: " + filename);
                Channels = b[2] | b[3] << 8;
                Rate = b[4] | b[5] << 8 | b[6] << 16 | (uint32_t)b[7] << 24;
                fmt = true;
                if (len & 1) std::fgetc(F);
            } else if (!std::memcmp(c, "data", 4)) {
                if (!fmt || !Channels)
                    throw std::runtime_error("bad WAV header: " + filename);
                Frames = len / (2 * Channels);
                break;
            } else {
                std::fseek(F, len + (len & 1), SEEK_CUR);
            }
        }
    }
    TPCMIOWav(const std::string& filename, int channels, int sampleRate)
        : Channels(channels), Rate(sampleRate), Writing(true) {
        F = std::fopen(filename.c_str(), "wb");
        if (!F)
            throw std::runtime_error("can't write " + filename);
        unsigned char h[44] = {0};
        std::fwrite(h, 1, 44, F);  // filled in when closing
    }
    ~TPCMIOWav() override {
        if (!F)
            return;
        if (Writing) {
            uint32_t data = Frames * 2 * Channels;
            unsigned char h[44];
            std::memcpy(h, "RIFF", 4); put32(h + 4, 36 + data); std::memcpy(h + 8, "WAVEfmt ", 8);
            put32(h + 16, 16); put16(h + 20, 1); put16(h + 22, Channels); put32(h + 24, Rate);
            put32(h + 28, Rate * 2 * Channels); put16(h + 32, 2 * Channels); put16(h + 34, 16);
            std::memcpy(h + 36, "data", 4); put32(h + 40, data);
            std::fseek(F, 0, SEEK_SET);
            std::fwrite(h, 1, 44, F);
        }
        std::fclose(F);
    }
    size_t GetChannelsNum() const override { return Channels; }
    size_t GetSampleRate() const override { return Rate; }
    size_t GetTotalSamples() const override { return Frames; }
    size_t Read(TPCMBuffer& buf, size_t sz) override {
        std::vector<int16_t> s(sz * Channels);
        size_t got = std::fread(s.data(), 2 * Channels, sz, F);  // little-endian, as x86
        float* out = buf[0];
        for (size_t i = 0; i < got * Channels; i++)
            out[i] = s[i] / 32768.0f;
        for (size_t i = got * Channels; i < sz * Channels; i++)
            out[i] = 0.0f;
        return got;
    }
    size_t Write(const TPCMBuffer& buf, size_t sz) override {
        std::vector<int16_t> s(sz * Channels);
        const float* in = const_cast<TPCMBuffer&>(buf)[0];
        for (size_t i = 0; i < sz * Channels; i++) {
            float v = in[i] * 32768.0f;
            s[i] = (int16_t)(v > 32767.0f ? 32767 : v < -32768.0f ? -32768 : v);
        }
        size_t n = std::fwrite(s.data(), 2 * Channels, sz, F);
        Frames += n;
        return n;
    }
private:
    static void put16(unsigned char* p, uint32_t v) { p[0] = v; p[1] = v >> 8; }
    static void put32(unsigned char* p, uint32_t v) { p[0] = v; p[1] = v >> 8; p[2] = v >> 16; p[3] = v >> 24; }
    std::FILE* F = nullptr;
    uint32_t Channels = 0, Rate = 0, Frames = 0;
    bool Writing = false;
};

IPCMProviderImpl* CreatePCMIOReadImpl(const std::string& path) {
    return new TPCMIOWav(path);
}

IPCMProviderImpl* CreatePCMIOWriteImpl(const std::string& path, int channels, int sampleRate) {
    return new TPCMIOWav(path, channels, sampleRate);
}
