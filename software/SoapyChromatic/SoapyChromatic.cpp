// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// SoapySDR module for the Chromatic SDR: ESP-SDR protocol (https://espargos.net/espsdr/, ESPARGOS:
// Florian Euchner) over the Chromatic USB CDC port, relayed
// by firmware/sdr (--with-app USB link: capture payloads at the USB rate) or the ESP32 directly
// (standard bitstream USB bridge, 2Mbaud).
//
// The ESP32 captures bursts (up to 16380 samples, 8-bit I/Q, 16/40MS/s): the stream is a sequence
// of bursts, each one starting with a time (SOAPY_SDR_HAS_TIME: host time at reception) and ending
// with SOAPY_SDR_END_BURST; there are gaps between bursts. Bursts captured before a settings change
// (frequency, gain, rate) are dropped.
//
// Tuning: with the Chromatic ESP-SDR fork (firmware/esp32-sdr: FREQK), the ESP32 tunes
// 2150-2880MHz in kHz steps (and 80MS/s with its RX filter opened: +-38MHz); with stock ESP-SDR,
// only the Wi-Fi channel frequencies (2412-2472MHz/5MHz, 2484MHz) move its LO (measured on the
// console crystal harmonics): the nearest channel is used. The remaining offset is applied by a
// digital mixer (NCO). The ESP32 I/Q spectrum is inverted: the
// samples are conjugated.
//
// Device arguments: driver=chromatic, serial=<port> (default: the Chromatic CDC port),
// samples=<burst samples> (default 16380).

#include <SoapySDR/Device.hpp>
#include <SoapySDR/Formats.hpp>
#include <SoapySDR/Logger.hpp>
#include <SoapySDR/Registry.hpp>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <complex>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include <fcntl.h>
#include <glob.h>
#include <poll.h>
#include <sys/ioctl.h>
#include <termios.h>
#include <unistd.h>

// Debug trace (CHROMATIC_SOAPY_DEBUG=1) -----------------------------------------------------------

static bool debug_enabled()
{
    static bool enabled = std::getenv("CHROMATIC_SOAPY_DEBUG") != nullptr;
    return enabled;
}

#define TRACE(...) do { if (debug_enabled()) { fprintf(stderr, "[chromatic] " __VA_ARGS__); \
    fprintf(stderr, "\n"); } } while (0)

// CRC32 ------------------------------------------------------------------------------------------

static uint32_t crc32(const uint8_t *data, size_t len)
{
    static uint32_t table[256];
    if (!table[1])
        for (uint32_t i = 0; i < 256; i++) {
            uint32_t c = i;
            for (int k = 0; k < 8; k++)
                c = (c & 1) ? 0xedb88320 ^ (c >> 1) : c >> 1;
            table[i] = c;
        }
    uint32_t crc = 0xffffffff;
    for (size_t i = 0; i < len; i++)
        crc = table[(crc ^ data[i]) & 0xff] ^ (crc >> 8);
    return crc ^ 0xffffffff;
}

// Serial Link ------------------------------------------------------------------------------------

class SerialLink
{
public:
    explicit SerialLink(const std::string &path)
    {
        fd = ::open(path.c_str(), O_RDWR | O_NOCTTY);
        if (fd < 0)
            throw std::runtime_error("Chromatic: can't open " + path);
        // Standard bitstream bridge: DTR/RTS drive the ESP32 EN/IO0, keep them released.
        int lines = TIOCM_DTR | TIOCM_RTS;
        ioctl(fd, TIOCMBIC, &lines);
        struct termios tio;
        tcgetattr(fd, &tio);
        cfmakeraw(&tio);
        tio.c_cflag |= CLOCAL | CREAD;
        tio.c_cflag &= ~HUPCL;
        cfsetispeed(&tio, B2000000); // ESP32 UART rate (bridge), ignored by the USB relay.
        cfsetospeed(&tio, B2000000);
        tcsetattr(fd, TCSANOW, &tio);
        tcflush(fd, TCIOFLUSH);
    }

    ~SerialLink() { ::close(fd); }

    void write(const std::string &s)
    {
        const char *p = s.data();
        size_t      n = s.size();
        while (n) {
            ssize_t r = ::write(fd, p, n);
            if (r <= 0)
                throw std::runtime_error("Chromatic: write error");
            p += r;
            n -= r;
        }
    }

    // Read exactly len bytes (false on timeout).
    bool read(uint8_t *buf, size_t len, int timeout_ms)
    {
        auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
        size_t got = 0;
        while (got < len) {
            if (pending_pos < pending.size()) {
                size_t n = std::min(len - got, pending.size() - pending_pos);
                memcpy(buf + got, pending.data() + pending_pos, n);
                pending_pos += n;
                got += n;
                continue;
            }
            int left = (int)std::chrono::duration_cast<std::chrono::milliseconds>(
                deadline - std::chrono::steady_clock::now()).count();
            if (left <= 0 || !fill(left))
                return false;
        }
        return true;
    }

    // Read a line without its newline (false on timeout).
    bool readline(std::string &line, int timeout_ms)
    {
        line.clear();
        uint8_t c;
        while (read(&c, 1, timeout_ms)) {
            if (c == '\n')
                return true;
            if (c != '\r')
                line += (char)c;
        }
        return false;
    }

    void drain(int quiet_ms)
    {
        pending.clear();
        pending_pos = 0;
        while (fill(quiet_ms))
            pending.clear(), pending_pos = 0;
    }

private:
    bool fill(int timeout_ms)
    {
        struct pollfd p = {fd, POLLIN, 0};
        if (poll(&p, 1, timeout_ms) <= 0)
            return false;
        uint8_t buf[65536];
        ssize_t r = ::read(fd, buf, sizeof(buf));
        if (r <= 0)
            return false;
        if (pending_pos == pending.size()) {
            pending.clear();
            pending_pos = 0;
        }
        pending.insert(pending.end(), buf, buf + r);
        return true;
    }

    int                  fd;
    std::vector<uint8_t> pending;
    size_t               pending_pos = 0;
};

// Chromatic SDR ----------------------------------------------------------------------------------

struct Burst
{
    std::vector<int8_t> iq;
    long long           timeNs;
    unsigned            generation; // Settings generation at the capture.
    double              offset;     // NCO frequency offset (Hz).
    double              rate;
};

class ChromaticSDR : public SoapySDR::Device
{
public:
    explicit ChromaticSDR(const SoapySDR::Kwargs &args) : link(args.at("serial"))
    {
        if (args.count("samples"))
            burst_samples = std::max(256, std::min(16380, std::stoi(args.at("samples"))));
        sync();
        info = command("INFO");
        unsigned gmax;
        std::string limits = command("LIMITS?");
        auto g = limits.find("\"gain\":[0,");
        if (g != std::string::npos && sscanf(limits.c_str() + g, "\"gain\":[0,%u", &gmax) == 1)
            gain_max = gmax;
        // kHz tuning (Chromatic ESP-SDR fork: 2386-2504MHz), else Wi-Fi channels + NCO.
        unsigned kmin, kmax;
        if (sscanf(command("RANGEK?").c_str(), "RANGEK %u %u", &kmin, &kmax) == 2) {
            freqk    = true;
            freq_min = kmin*1e3;
            freq_max = kmax*1e3;
        }
        setFrequency(SOAPY_SDR_RX, 0, freq);
        setGainMode(SOAPY_SDR_RX, 0, true);
    }

    ~ChromaticSDR() override { stop(); }

    // Identification.
    std::string getDriverKey() const override { return "chromatic"; }
    std::string getHardwareKey() const override { return "ChromaticESPSDR"; }
    SoapySDR::Kwargs getHardwareInfo() const override { return {{"info", info}}; }

    // Channels.
    size_t getNumChannels(const int direction) const override
    {
        return (direction == SOAPY_SDR_RX) ? 1 : 0;
    }

    // Stream formats.
    std::vector<std::string> getStreamFormats(const int, const size_t) const override
    {
        return {SOAPY_SDR_CS8, SOAPY_SDR_CS16, SOAPY_SDR_CF32};
    }

    std::string getNativeStreamFormat(const int, const size_t, double &fullScale) const override
    {
        fullScale = 128;
        return SOAPY_SDR_CS8;
    }

    // Antenna.
    std::vector<std::string> listAntennas(const int, const size_t) const override { return {"RX"}; }
    std::string getAntenna(const int, const size_t) const override { return "RX"; }

    // Frequency (MHz steps).
    void setFrequency(const int, const size_t, const double frequency,
        const SoapySDR::Kwargs & = SoapySDR::Kwargs()) override
    {
        TRACE("setFrequency(%.0f)", frequency);
        if (freqk) {
            // ESP32 at the nearest kHz, offset by the NCO.
            double   f   = std::max(freq_min, std::min(freq_max, frequency));
            unsigned khz = (unsigned)(f/1e3 + 0.5);
            if (khz*1e3 != hw_freq) {
                std::string r = command("FREQK " + std::to_string(khz));
                if (r != "OK")
                    SoapySDR::logf(SOAPY_SDR_ERROR, "Chromatic: FREQK %u: %s", khz, r.c_str());
                hw_freq = khz*1e3;
            }
            freq = f;
            generation++;
            return;
        }
        // Stock ESP-SDR: ESP32 at the nearest Wi-Fi channel, offset by the NCO.
        static const unsigned channels[] = {
            2412, 2417, 2422, 2427, 2432, 2437, 2442, 2447, 2452, 2457, 2462, 2467, 2472, 2484,
        };
        double   f   = std::max(freq_min, std::min(freq_max, frequency));
        unsigned mhz = channels[0];
        for (unsigned ch : channels)
            if (std::fabs(ch*1e6 - f) < std::fabs(mhz*1e6 - f))
                mhz = ch;
        if (mhz*1e6 != hw_freq) {
            std::string r = command("FREQ " + std::to_string(mhz));
            if (r != "OK")
                SoapySDR::logf(SOAPY_SDR_ERROR, "Chromatic: FREQ %u: %s", mhz, r.c_str());
            hw_freq = mhz*1e6;
        }
        freq = f;
        generation++;
    }

    double getFrequency(const int, const size_t) const override { return freq; }

    SoapySDR::RangeList getFrequencyRange(const int, const size_t) const override
    {
        return {SoapySDR::Range(freq_min, freq_max)};
    }

    // Sample rate (ESP32 hardware rates).
    std::vector<double> listSampleRates(const int, const size_t) const override
    {
        // 80MS/s (RX filter opened: +-38MHz usable) with the Chromatic ESP-SDR fork.
        if (freqk)
            return {16e6, 40e6, 80e6};
        return {16e6, 40e6};
    }

    void setSampleRate(const int, const size_t, const double r) override
    {
        rate = (r < 28e6) ? 16e6 : (r < 60e6 || !freqk) ? 40e6 : 80e6;
        command((rate == 80e6) ? "LPF 0" : "LPF AUTO");
        TRACE("setSampleRate(%.0f) -> %.0f", r, rate);
        generation++;
    }

    double getSampleRate(const int, const size_t) const override { return rate; }

    SoapySDR::RangeList getBandwidthRange(const int, const size_t) const override
    {
        return {SoapySDR::Range(16e6, freqk ? 76e6 : 40e6)};
    }

    double getBandwidth(const int, const size_t) const override { return rate; }

    // Gain (AGC or manual).
    bool hasGainMode(const int, const size_t) const override { return true; }

    void setGainMode(const int, const size_t, const bool automatic) override
    {
        agc = automatic;
        generation++;
        if (agc)
            command("GAIN HARDWARE");
        else
            setGain(SOAPY_SDR_RX, 0, gain);
    }

    bool getGainMode(const int, const size_t) const override { return agc; }

    void setGain(const int, const size_t, const double value) override
    {
        gain = std::max(0.0, std::min((double)gain_max, value));
        agc  = false;
        generation++;
        command("GAIN MANUAL " + std::to_string((unsigned)gain));
    }

    double getGain(const int, const size_t) const override { return gain; }

    SoapySDR::Range getGainRange(const int, const size_t) const override
    {
        return SoapySDR::Range(0, gain_max, 1);
    }

    // Single gain element (gqrx/gr-osmosdr gain sliders).
    std::vector<std::string> listGains(const int, const size_t) const override { return {"LNA"}; }

    void setGain(const int direction, const size_t channel, const std::string &,
        const double value) override
    {
        setGain(direction, channel, value);
    }

    double getGain(const int direction, const size_t channel, const std::string &) const override
    {
        return getGain(direction, channel);
    }

    SoapySDR::Range getGainRange(const int direction, const size_t channel,
        const std::string &) const override
    {
        return getGainRange(direction, channel);
    }

    // Stream.
    SoapySDR::Stream *setupStream(const int direction, const std::string &format,
        const std::vector<size_t> &channels, const SoapySDR::Kwargs &) override
    {
        if (direction != SOAPY_SDR_RX || channels.size() > 1 || (channels.size() && channels[0]))
            throw std::runtime_error("Chromatic: RX channel 0 only");
        if (format != SOAPY_SDR_CS8 && format != SOAPY_SDR_CS16 && format != SOAPY_SDR_CF32)
            throw std::runtime_error("Chromatic: unsupported format " + format);
        stream_format = format;
        return reinterpret_cast<SoapySDR::Stream *>(this);
    }

    void closeStream(SoapySDR::Stream *) override { stop(); }

    size_t getStreamMTU(SoapySDR::Stream *) const override { return burst_samples; }

    int activateStream(SoapySDR::Stream *, const int, const long long, const size_t) override
    {
        TRACE("activateStream (%s, %.0f Hz, %.0f S/s)", stream_format.c_str(), freq, rate);
        stop();
        {
            std::lock_guard<std::mutex> lock(queue_mutex);
            queue.clear();
            current.reset();
        }
        running = true;
        thread  = std::thread(&ChromaticSDR::capture_loop, this);
        return 0;
    }

    int deactivateStream(SoapySDR::Stream *, const int, const long long) override
    {
        stop();
        return 0;
    }

    int readStream(SoapySDR::Stream *, void *const *buffs, const size_t numElems, int &flags,
        long long &timeNs, const long timeoutUs) override
    {
        flags = 0;
        std::unique_lock<std::mutex> lock(queue_mutex);
        if (!current) {
            if (!queue_cv.wait_for(lock, std::chrono::microseconds(timeoutUs),
                [this] { return !queue.empty() || overflow; }))
                return SOAPY_SDR_TIMEOUT;
            if (overflow) {
                overflow = false;
                return SOAPY_SDR_OVERFLOW;
            }
            // Bursts captured before a settings change dropped.
            auto fresh = [this] {
                while (!queue.empty() && queue.front().generation != generation)
                    queue.pop_front();
                return !queue.empty();
            };
            if (!fresh() && !queue_cv.wait_for(lock, std::chrono::microseconds(timeoutUs), fresh))
                return SOAPY_SDR_TIMEOUT;
            current.reset(new Burst(std::move(queue.front())));
            queue.pop_front();
            offset = 0;
            nco    = 1.0;
            nco_step = std::polar(1.0, -2*M_PI*current->offset/current->rate);
            flags |= SOAPY_SDR_HAS_TIME;
            timeNs = current->timeNs;
        }
        lock.unlock();

        size_t total = current->iq.size()/2;
        size_t n     = std::min(numElems, total - offset);
        const int8_t *src = current->iq.data() + 2*offset;
        bool mix = (current->offset != 0);
        {
            for (size_t i = 0; i < n; i++) {
                std::complex<double> x(src[2*i + 0], -src[2*i + 1]); // Conjugated (inverted).
                if (mix) {
                    x   *= nco;
                    nco *= nco_step;
                }
                if (stream_format == SOAPY_SDR_CF32) {
                    ((float *)buffs[0])[2*i + 0] = (float)(x.real()/128);
                    ((float *)buffs[0])[2*i + 1] = (float)(x.imag()/128);
                } else if (stream_format == SOAPY_SDR_CS16) {
                    ((int16_t *)buffs[0])[2*i + 0] = (int16_t)std::lround(x.real()*256);
                    ((int16_t *)buffs[0])[2*i + 1] = (int16_t)std::lround(x.imag()*256);
                } else {
                    auto clip = [](double v) {
                        return (int8_t)std::max(-128L, std::min(127L, std::lround(v)));
                    };
                    ((int8_t *)buffs[0])[2*i + 0] = clip(x.real());
                    ((int8_t *)buffs[0])[2*i + 1] = clip(x.imag());
                }
            }
            nco /= std::abs(nco); // Renormalized (rounding errors).
        }
        offset += n;
        if (offset == total) {
            flags |= SOAPY_SDR_END_BURST;
            current.reset();
        }
        return (int)n;
    }

private:
    // Protocol (serialized with the capture thread).
    std::string command(const std::string &cmd)
    {
        TRACE("command: %s", cmd.c_str());
        std::lock_guard<std::mutex> lock(link_mutex);
        link.write(cmd + "\n");
        std::string reply;
        if (!link.readline(reply, 1000))
            SoapySDR::logf(SOAPY_SDR_WARNING, "Chromatic: no reply to %s", cmd.c_str());
        return reply;
    }

    void sync()
    {
        std::lock_guard<std::mutex> lock(link_mutex);
        for (int attempt = 0; attempt < 4; attempt++) {
            link.drain(50);
            std::string nonce = std::to_string(1000 + attempt), reply;
            link.write("SYNC " + nonce + "\n");
            while (link.readline(reply, 1000))
                if (reply == "SYNC " + nonce)
                    return;
        }
        throw std::runtime_error("Chromatic: ESP-SDR not responding");
    }

    void capture_loop()
    {
        while (running) {
            Burst burst;
            bool  ok = false;
            {
                std::lock_guard<std::mutex> lock(link_mutex);
                burst.generation = generation;
                burst.offset     = freq - hw_freq;
                burst.rate       = rate;
                link.write("CAP16 " + std::to_string(burst_samples) + " " +
                    ((rate == 16e6) ? "6" : (rate == 80e6) ? "0" : "1") + "\n");
                std::string header;
                unsigned n, us;
                char crc_hex[16];
                if (link.readline(header, 1000) &&
                    sscanf(header.c_str(), "DATA %u %15s %u", &n, crc_hex, &us) == 3) {
                    burst.timeNs = std::chrono::duration_cast<std::chrono::nanoseconds>(
                        std::chrono::steady_clock::now().time_since_epoch()).count();
                    burst.iq.resize(2*n);
                    const uint8_t *data = (const uint8_t *)burst.iq.data();
                    uint32_t       crc  = std::strtoul(crc_hex, nullptr, 16);
                    ok = link.read((uint8_t *)burst.iq.data(), 2*n, 1000) &&
                        crc32(data, 2*n) == crc;
                }
                if (!ok)
                    SoapySDR::logf(SOAPY_SDR_WARNING, "Chromatic: capture error (%s)",
                        header.c_str());
            }
            if (!ok) {
                try { sync(); } catch (...) {}
                continue;
            }
            std::lock_guard<std::mutex> lock(queue_mutex);
            if (queue.size() >= 64) {
                queue.clear();
                overflow = true;
            }
            queue.push_back(std::move(burst));
            queue_cv.notify_one();
        }
    }

    void stop()
    {
        running = false;
        if (thread.joinable())
            thread.join();
    }

    // Link (protocol commands serialized with the capture thread).
    SerialLink  link;
    std::mutex  link_mutex;
    std::string info;

    // Settings.
    double                freq          = 2437e6; // Requested frequency (Hz).
    double                hw_freq       = 0;      // ESP32 frequency (Hz), NCO for the rest.
    double                freq_min      = 2410e6;
    double                freq_max      = 2486e6;
    bool                  freqk         = false;  // kHz tuning (Chromatic ESP-SDR fork).
    double                rate          = 40e6;
    double                gain          = 40;
    unsigned              gain_max      = 72;
    bool                  agc           = true;
    int                   burst_samples = 16380;
    std::string           stream_format = SOAPY_SDR_CF32;
    std::atomic<unsigned> generation{0};          // Settings generation (stale bursts dropped).

    // Stream: capture thread -> bursts queue -> readStream.
    std::thread             thread;
    std::atomic<bool>       running{false};
    std::mutex              queue_mutex;
    std::condition_variable queue_cv;
    std::deque<Burst>       queue;
    bool                    overflow = false;
    std::unique_ptr<Burst>  current;         // Burst being read.
    size_t                  offset   = 0;    // Read position in the current burst (samples).
    std::complex<double>    nco{1.0};        // NCO phase.
    std::complex<double>    nco_step{1.0};   // NCO phase increment per sample.
};

// Registration -----------------------------------------------------------------------------------

static std::string default_port()
{
    glob_t g;
    std::string port;
    if (glob("/dev/serial/by-id/*Chromatic*if02*", 0, nullptr, &g) == 0 && g.gl_pathc)
        port = g.gl_pathv[0];
    globfree(&g);
    return port;
}

static SoapySDR::KwargsList find_chromatic(const SoapySDR::Kwargs &args)
{
    if (args.count("driver") && args.at("driver") != "chromatic")
        return {};
    std::string port = args.count("serial") ? args.at("serial") : default_port();
    if (port.empty())
        return {};
    SoapySDR::Kwargs dev;
    dev["driver"] = "chromatic";
    dev["serial"] = port;
    dev["label"]  = "Chromatic SDR (ESP-SDR) " + port;
    if (args.count("samples"))
        dev["samples"] = args.at("samples");
    return {dev};
}

static SoapySDR::Device *make_chromatic(const SoapySDR::Kwargs &args)
{
    return new ChromaticSDR(args);
}

static SoapySDR::Registry register_chromatic("chromatic", &find_chromatic, &make_chromatic,
    SOAPY_SDR_ABI_VERSION);
