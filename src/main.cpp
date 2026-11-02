// Anvil Fletch - local agent console for Windows.
// A single custom-painted window (sidebar, live agent transcript, composer)
// speaking JSON-lines to `python src/agent_backend.py --serve` over a pipe.

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#define _WIN32_WINNT 0x0A00
#include <windows.h>
#include <windowsx.h>
#include <commctrl.h>
#include <dwmapi.h>
#include <uxtheme.h>
#include <shellapi.h>
#include <objidl.h>
#include <gdiplus.h>
#include <shobjidl.h>
#include <string>
#include <vector>
#include <deque>
#include <map>
#include <memory>
#include <thread>
#include <atomic>
#include <mutex>
#include <fstream>
#include <algorithm>
#include <cstdint>
#include <cmath>
#include <cstdio>
#include <cwctype>

#pragma comment(lib, "comctl32.lib")
#pragma comment(lib, "dwmapi.lib")
#pragma comment(lib, "uxtheme.lib")
#pragma comment(lib, "gdi32.lib")
#pragma comment(lib, "user32.lib")
#pragma comment(lib, "shell32.lib")
#pragma comment(lib, "gdiplus.lib")
#pragma comment(lib, "ole32.lib")

using namespace Gdiplus;

// ---------------------------------------------------------------------------
// ids
// ---------------------------------------------------------------------------
#define ID_COMPOSER   401
#define WM_AGENT_LINE (WM_APP + 1)
#define WM_AGENT_DONE (WM_APP + 2)
#define WM_TICK       (WM_APP + 3)
#define WM_AUTOGROW   (WM_APP + 4)
#define WM_STUDY_START (WM_APP + 5)
#define WM_TRAYICON   (WM_APP + 6)

enum Hot {
    HOT_NONE = 0, HOT_NEW, HOT_WS, HOT_MODEL, HOT_STUDY, HOT_RECENT_BASE = 100,
    HOT_TURN_BASE = 1000, HOT_STEP_BASE = 2000,
    HOT_PICK_BASE = 60000, HOT_SUGGEST_BASE = 61000,
    HOT_STOP = 70000, HOT_SEND,
    HOT_COPY_TURN_BASE = 71000, HOT_COPY_RECENT_BASE = 72000,
    HOT_CLEAR_RECENTS = 73000, HOT_ATTACH, HOT_ATTACH_BASE = 74000,
};

// ---------------------------------------------------------------------------
// theme - forged steel and ember
// ---------------------------------------------------------------------------
static const COLORREF C_APP     = RGB(14, 13, 12);
static const COLORREF C_SIDE    = RGB(20, 19, 17);
static const COLORREF C_SHEET   = RGB(16, 15, 13);
static const COLORREF C_PANEL   = RGB(27, 25, 21);
static const COLORREF C_WELL    = RGB(19, 18, 16);
static const COLORREF C_CARD    = RGB(34, 31, 26);
static const COLORREF C_CARD_H  = RGB(42, 38, 31);
static const COLORREF C_LINE    = RGB(46, 42, 35);
static const COLORREF C_LINE2   = RGB(62, 56, 46);
static const COLORREF C_TEXT    = RGB(241, 236, 228);
static const COLORREF C_MUTED   = RGB(171, 161, 146);
static const COLORREF C_DIM     = RGB(118, 109, 97);
static const COLORREF C_ACCENT  = RGB(242, 160, 61);
static const COLORREF C_ACCENT_H= RGB(255, 187, 99);
static const COLORREF C_EMBER   = RGB(224, 102, 46);
static const COLORREF C_OK      = RGB(123, 197, 138);
static const COLORREF C_ERR     = RGB(226, 104, 92);
static const COLORREF C_WARN    = RGB(224, 169, 60);
static const COLORREF C_THINK   = RGB(201, 161, 107);
static const COLORREF C_CODE    = RGB(240, 194, 125);
static const COLORREF C_USERBG  = RGB(30, 26, 21);
static const COLORREF C_USERBRD = RGB(56, 47, 37);
static const COLORREF C_INK     = RGB(20, 15, 8);

static HFONT F_TITLE = NULL, F_H1 = NULL, F_H2 = NULL, F_UI = NULL, F_UIB = NULL,
             F_SMALL = NULL, F_SMALLB = NULL, F_LABEL = NULL, F_CHAT = NULL,
             F_MONO = NULL, F_MONOS = NULL, F_ITALIC = NULL, F_GLYPH = NULL;

static const int SIDE_W = 300;
static const int HEAD_H = 64;
static const int PAD    = 36;
static const int LH_CHAT = 25, LH_MONO = 18;

// ---------------------------------------------------------------------------
// utf helpers
// ---------------------------------------------------------------------------
static std::wstring U8(const std::string& s) {
    if (s.empty()) return L"";
    int n = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), NULL, 0);
    std::wstring w(n, 0);
    MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), &w[0], n);
    return w;
}
static std::string W8(const std::wstring& w) {
    if (w.empty()) return "";
    int n = WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), NULL, 0, NULL, NULL);
    std::string s(n, 0);
    WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), &s[0], n, NULL, NULL);
    return s;
}
static std::wstring Trim(const std::wstring& s) {
    size_t a = s.find_first_not_of(L" \t\r\n"), b = s.find_last_not_of(L" \t\r\n");
    return (a == std::wstring::npos) ? L"" : s.substr(a, b - a + 1);
}
static bool IEquals(const std::wstring& a, const std::wstring& b) {
    if (a.size() != b.size()) return false;
    for (size_t i = 0; i < a.size(); ++i) if (towlower(a[i]) != towlower(b[i])) return false;
    return true;
}
static std::wstring Ellipsize(HDC hdc, const std::wstring& s, int maxW, HFONT f) {
    HFONT old = (HFONT)SelectObject(hdc, f);
    SIZE sz{};
    GetTextExtentPoint32W(hdc, s.c_str(), (int)s.size(), &sz);
    if (sz.cx <= maxW) { SelectObject(hdc, old); return s; }
    std::wstring t = s;
    while (!t.empty()) {
        t.pop_back();
        std::wstring c = t + L"...";
        GetTextExtentPoint32W(hdc, c.c_str(), (int)c.size(), &sz);
        if (sz.cx <= maxW) { SelectObject(hdc, old); return c; }
    }
    SelectObject(hdc, old);
    return L"...";
}
static std::wstring BaseName(const std::wstring& p) {
    size_t sl = p.find_last_of(L"\\/");
    return sl == std::wstring::npos ? p : p.substr(sl + 1);
}

static std::wstring g_root;

// ---------------------------------------------------------------------------
// minimal JSON
// ---------------------------------------------------------------------------
struct JV {
    enum T { Null, Str, Num, Bool, Arr, Obj } t = Null;
    std::wstring s; double n = 0; bool b = false;
    std::vector<JV> arr;
    std::map<std::wstring, JV> obj;
    const JV* at(const wchar_t* k) const {
        auto i = obj.find(k); return i == obj.end() ? nullptr : &i->second;
    }
    std::wstring str(const wchar_t* k, const std::wstring& d = L"") const {
        const JV* v = at(k); return (v && v->t == JV::Str) ? v->s : d;
    }
    double num(const wchar_t* k, double d = 0) const {
        const JV* v = at(k); return (v && v->t == JV::Num) ? v->n : d;
    }
    bool flag(const wchar_t* k, bool d = false) const {
        const JV* v = at(k); return (v && v->t == JV::Bool) ? v->b : d;
    }
    const std::vector<JV>* list(const wchar_t* k) const {
        const JV* v = at(k); return (v && v->t == JV::Arr) ? &v->arr : nullptr;
    }
};

struct JsonParser {
    const std::wstring& src; size_t i = 0;
    explicit JsonParser(const std::wstring& s) : src(s) {}
    void ws() { while (i < src.size() && iswspace(src[i])) ++i; }
    bool fail = false;

    JV value() {
        JV v; ws();
        if (i >= src.size()) { fail = true; return v; }
        wchar_t c = src[i];
        if (c == L'{') return object();
        if (c == L'[') return array();
        if (c == L'"') { v.t = JV::Str; v.s = text(); return v; }
        if (c == L't' || c == L'f') {
            v.t = JV::Bool;
            if (src.compare(i, 4, L"true") == 0) { v.b = true; i += 4; }
            else if (src.compare(i, 5, L"false") == 0) { v.b = false; i += 5; }
            else fail = true;
            return v;
        }
        if (c == L'n') { i += 4; return v; }
        size_t st = i;
        while (i < src.size() && (iswdigit(src[i]) || src[i] == L'-' || src[i] == L'+' ||
                                  src[i] == L'.' || src[i] == L'e' || src[i] == L'E')) ++i;
        if (st == i) { fail = true; return v; }
        v.t = JV::Num; v.n = _wtof(src.substr(st, i - st).c_str());
        return v;
    }
    std::wstring text() {
        std::wstring out;
        if (i >= src.size() || src[i] != L'"') { fail = true; return out; }
        ++i;
        while (i < src.size()) {
            wchar_t c = src[i++];
            if (c == L'"') return out;
            if (c == L'\\' && i < src.size()) {
                wchar_t e = src[i++];
                switch (e) {
                case L'n': out += L'\n'; break;
                case L'r': out += L'\r'; break;
                case L't': out += L'\t'; break;
                case L'b': out += L'\b'; break;
                case L'f': out += L'\f'; break;
                case L'/': out += L'/'; break;
                case L'\\': out += L'\\'; break;
                case L'"': out += L'"'; break;
                case L'u': {
                    if (i + 4 <= src.size()) {
                        std::wstring h = src.substr(i, 4); i += 4;
                        out += (wchar_t)wcstol(h.c_str(), NULL, 16);
                    }
                    break;
                }
                default: out += e;
                }
            } else out += c;
        }
        fail = true;
        return out;
    }
    JV object() {
        JV v; v.t = JV::Obj; ++i; ws();
        if (i < src.size() && src[i] == L'}') { ++i; return v; }
        while (i < src.size()) {
            ws();
            std::wstring k = text();
            ws();
            if (i >= src.size() || src[i] != L':') { fail = true; return v; }
            ++i;
            v.obj[k] = value();
            ws();
            if (i < src.size() && src[i] == L',') { ++i; continue; }
            if (i < src.size() && src[i] == L'}') { ++i; return v; }
            fail = true; return v;
        }
        fail = true; return v;
    }
    JV array() {
        JV v; v.t = JV::Arr; ++i; ws();
        if (i < src.size() && src[i] == L']') { ++i; return v; }
        while (i < src.size()) {
            v.arr.push_back(value());
            ws();
            if (i < src.size() && src[i] == L',') { ++i; continue; }
            if (i < src.size() && src[i] == L']') { ++i; return v; }
            fail = true; return v;
        }
        fail = true; return v;
    }
};

static JV ParseJson(const std::wstring& s) {
    JsonParser p(s);
    return p.value();
}

static std::wstring JEsc(const std::wstring& s) {
    std::wstring o;
    for (wchar_t c : s) {
        switch (c) {
        case L'"': o += L"\\\""; break;
        case L'\\': o += L"\\\\"; break;
        case L'\n': o += L"\\n"; break;
        case L'\r': break;
        case L'\t': o += L"\\t"; break;
        default:
            if (c < 32) { wchar_t b[8]; swprintf(b, 8, L"\\u%04x", c); o += b; }
            else o += c;
        }
    }
    return o;
}

// ---------------------------------------------------------------------------
// transcript model
// ---------------------------------------------------------------------------
struct Run { std::wstring text; COLORREF col; HFONT font; };
struct Line { std::vector<Run> runs; int w = 0; int indent = 0; bool code = false; };

struct Step {
    enum K { Thought, Tool, Plan, Note } kind = Thought;
    std::wstring text;
    std::wstring name;
    std::wstring summary, detail;
    std::vector<std::pair<std::wstring, std::wstring>> args;
    std::vector<std::pair<std::wstring, std::wstring>> items;
    bool running = false, ok = true, expanded = false, stopped = false;
    long long ms = 0;
};

struct Turn {
    std::wstring user;
    std::vector<Line> userLines;
    int userW = -1;
    std::vector<Step> steps;
    std::wstring liveThought;
    std::wstring answer;
    std::vector<Line> answerLines;
    int answerW = -1; bool answerDirty = false;
    long long answerRenderedAt = 0; size_t answerRenderedLen = 0;
    bool running = false, expanded = false;
    bool studySession = false;
    long long started = 0, ms = 0;
    int tools = 0, fails = 0;
};

static std::vector<Turn> g_turns;
static std::vector<std::wstring> g_recents;
static std::vector<std::wstring> g_models;

// ---------------------------------------------------------------------------
// markdown-lite
// ---------------------------------------------------------------------------
static void SplitRuns(const std::wstring& src, COLORREF base, HFONT regular,
                      std::vector<Run>& out) {
    size_t i = 0;
    std::wstring plain;
    auto flush = [&]() {
        if (!plain.empty()) { out.push_back({plain, base, regular}); plain.clear(); }
    };
    while (i < src.size()) {
        if (src[i] == L'`') {
            size_t j = src.find(L'`', i + 1);
            if (j != std::wstring::npos && j > i + 1) {
                flush();
                out.push_back({src.substr(i + 1, j - i - 1), C_CODE, F_MONOS});
                i = j + 1; continue;
            }
        }
        if (src[i] == L'*' && i + 1 < src.size() && src[i + 1] == L'*') {
            size_t j = src.find(L"**", i + 2);
            if (j != std::wstring::npos) {
                flush();
                out.push_back({src.substr(i + 2, j - i - 2), C_TEXT, F_UIB});
                i = j + 2; continue;
            }
        }
        if ((src[i] == L'*' || src[i] == L'_') && i + 2 < src.size() &&
            src[i + 1] != L' ' && src[i + 1] != src[i]) {
            wchar_t d = src[i];
            size_t j = src.find(d, i + 2);
            if (j != std::wstring::npos && src[j - 1] != L' ' &&
                !(j + 1 < src.size() && src[j + 1] == d)) {
                bool okB = true;
                if (d == L'_') {
                    if (i > 0 && iswalnum(src[i - 1])) okB = false;
                    if (j + 1 < src.size() && iswalnum(src[j + 1])) okB = false;
                }
                if (okB) {
                    flush();
                    out.push_back({src.substr(i + 1, j - i - 1), base, F_ITALIC});
                    i = j + 1; continue;
                }
            }
        }
        if (src[i] == L'[') {
            size_t close = src.find(L']', i);
            size_t paren = (close == std::wstring::npos) ? std::wstring::npos : src.find(L'(', close);
            size_t end = (paren == std::wstring::npos) ? std::wstring::npos : src.find(L')', paren);
            if (end != std::wstring::npos) {
                flush();
                out.push_back({src.substr(i + 1, close - i - 1), C_ACCENT, regular});
                i = end + 1; continue;
            }
        }
        plain += src[i++];
    }
    flush();
}

static void WrapRuns(HDC hdc, const std::vector<Run>& runs, int maxW, int indentFirst,
                     int indentRest, std::vector<Line>& out) {
    Line cur; cur.indent = out.empty() ? indentFirst : indentRest;
    int x = cur.indent;
    auto pushLine = [&]() {
        if (!cur.runs.empty()) { cur.w = x; out.push_back(cur); }
        else if (out.empty() || !out.back().runs.empty()) out.push_back(Line{});
    };
    for (const Run& r : runs) {
        std::wstring tok;
        size_t i = 0;
        auto emitTok = [&](const std::wstring& piece) {
            if (piece.empty()) return;
            SIZE sz{};
            HFONT old = (HFONT)SelectObject(hdc, r.font);
            GetTextExtentPoint32W(hdc, piece.c_str(), (int)piece.size(), &sz);
            SelectObject(hdc, old);
            if (x + sz.cx > maxW && x > cur.indent) {
                pushLine();
                cur = Line(); cur.indent = indentRest;
                x = cur.indent;
            }
            cur.runs.push_back({piece, r.col, r.font});
            x += sz.cx;
        };
        while (i < r.text.size()) {
            wchar_t c = r.text[i];
            if (c == L'\r') { ++i; continue; }
            if (c == L'\n') {
                emitTok(tok); tok.clear();
                cur.w = x;
                out.push_back(cur);
                cur = Line(); cur.indent = indentRest; x = cur.indent;
                ++i; continue;
            }
            if (c == L' ') { tok += c; emitTok(tok); tok.clear(); ++i; continue; }
            tok += c; ++i;
        }
        emitTok(tok);
    }
    if (!cur.runs.empty()) { cur.w = x; out.push_back(cur); }
}

static void RenderMarkdown(HDC hdc, const std::wstring& md, int maxW, std::vector<Line>& out) {
    out.clear();
    size_t pos = 0;
    bool inCode = false;
    while (pos <= md.size()) {
        size_t nl = md.find(L'\n', pos);
        std::wstring raw = (nl == std::wstring::npos) ? md.substr(pos) : md.substr(pos, nl - pos);
        pos = (nl == std::wstring::npos) ? md.size() + 1 : nl + 1;
        bool done = (nl == std::wstring::npos);

        std::wstring trimmed = Trim(raw);
        if (trimmed.rfind(L"```", 0) == 0) { inCode = !inCode; if (done) break; continue; }

        if (inCode) {
            Line l; l.code = true;
            l.runs.push_back({raw, C_CODE, F_MONO});
            SIZE sz{};
            HFONT old = (HFONT)SelectObject(hdc, F_MONO);
            GetTextExtentPoint32W(hdc, raw.c_str(), (int)raw.size(), &sz);
            SelectObject(hdc, old);
            l.w = sz.cx + 28;
            out.push_back(l);
            if (done) break; continue;
        }
        if (trimmed.empty()) {
            out.push_back(Line());
            if (done) break; continue;
        }

        int indent = 0; COLORREF col = C_TEXT; HFONT f = F_CHAT;
        std::wstring prefix; COLORREF pcol = C_MUTED;
        std::wstring body = trimmed;

        if (body[0] == L'#') {
            size_t h = 0; while (h < body.size() && body[h] == L'#') ++h;
            if (h <= 4 && h < body.size() && body[h] == L' ') {
                body = Trim(body.substr(h));
                f = F_H2;
                indent = (h <= 2) ? 2 : 0;
            }
        } else if (body.size() >= 2 && (body[0] == L'-' || body[0] == L'*' || body[0] == L'+')
                   && body[1] == L' ') {
            body = Trim(body.substr(2));
            prefix = L"\x2022   "; pcol = C_ACCENT; indent = 20;
        } else if (body.size() >= 3 && iswdigit(body[0])) {
            size_t k = 0;
            while (k < body.size() && iswdigit(body[k])) ++k;
            if (k < body.size() && (body[k] == L'.' || body[k] == L')' || body[k] == L'|')
                && k + 1 < body.size() && body[k + 1] == L' ') {
                prefix = body.substr(0, k) + L".   ";
                body = Trim(body.substr(k + 2));
                indent = 26;
            }
        } else if (body.size() >= 2 && body[0] == L'>' && body[1] == L' ') {
            body = Trim(body.substr(2));
            prefix = L"|   "; pcol = C_LINE2; col = C_MUTED; indent = 16;
        }

        std::vector<Run> runs;
        if (!prefix.empty()) runs.push_back({prefix, pcol, F_UIB});
        SplitRuns(body, col, f, runs);
        if (runs.empty()) runs.push_back({L" ", col, f});
        WrapRuns(hdc, runs, maxW - indent, indent, indent, out);
        if (done) break;
    }
    while (!out.empty() && out.back().runs.empty()) out.pop_back();
}

// ---------------------------------------------------------------------------
// app state
// ---------------------------------------------------------------------------
static HWND g_hwnd = NULL, g_edit = NULL;
static HANDLE g_proc = NULL, g_stdinWr = NULL;
static std::mutex g_qmu;
static std::deque<std::wstring> g_q;
static std::atomic<bool> g_busy{ false }, g_dead{ false }, g_booting{ false };
static std::wstring g_pending;
static bool g_pendingStudy = false;
static bool g_studyStartPending = false;
static std::wstring g_status = L"ready";
static std::wstring g_wsPath, g_model = L"qwen2.5-coder:7b";
static int g_toolCount = 0, g_lessonCount = 0;
static bool g_online = false, g_warmed = false, g_study = false;
static int g_scroll = 0, g_contentH = 0;
static int g_compH = 92;
static bool g_follow = true;
static int g_hot = HOT_NONE, g_hover = HOT_NONE;
static bool g_modelMenu = false;

static std::wstring g_toast;
static ULONGLONG g_toastUntil = 0;
static ULONGLONG g_lastPaintAt = 0;
static int g_paintCostMs = 0;
static bool g_paintAgain = false;
static unsigned long long g_uiRev = 0;
static unsigned long long g_lastPaintRev = 0;
static std::atomic<bool> g_linePosted{ false };
static NOTIFYICONDATAW g_nid{};
static bool g_trayAdded = false;

static std::vector<std::wstring> g_attachments;

// Spinner-only frames reuse the last full render instead of rebuilding the whole
// scene: layout plus GDI text is the expensive part, and it does not change.
struct SpinnerSpot { float cx, cy; COLORREF c; float s; };
static std::vector<SpinnerSpot> g_spinners;
static bool g_recordSpinners = false;
static HBITMAP g_frameBmp = NULL;
static HDC g_frameDC = NULL;
static int g_frameW = 0, g_frameH = 0;
static bool g_frameValid = false;

struct ProgressState {
    std::wstring label, item;
    long long done = 0, total = 0, filesDone = 0, filesTotal = 0;
    ULONGLONG until = 0;
    bool active = false;
} g_prog;

static HBRUSH g_brApp = NULL, g_brEdit = NULL;
static HFONT g_editFont = NULL;

static long long NowMs() {
    LARGE_INTEGER f, t; QueryPerformanceFrequency(&f); QueryPerformanceCounter(&t);
    return (long long)(t.QuadPart * 1000 / f.QuadPart);
}

static std::wstring DurStr(long long ms) {
    wchar_t b[32];
    if (ms < 1000) swprintf(b, 32, L"%lldms", ms);
    else if (ms < 60000) swprintf(b, 32, L"%.1fs", ms / 1000.0);
    else swprintf(b, 32, L"%lldm %02llds", ms / 60000, (ms % 60000) / 1000);
    return b;
}
static std::wstring ClockStr(long long ms) {
    wchar_t b[32];
    long long s = ms / 1000;
    swprintf(b, 32, L"%lld:%02lld", s / 60, s % 60);
    return b;
}

static void Dispatch(const std::wstring& prompt, bool studySession = false);
static void StartTurn(const std::wstring& prompt, bool studySession = false);
static std::wstring StudyPrompt() {
    return L"Study session \x2014 C++ and competitive programming. Keep learning until I stop.";
}
static void AutoGrow();

static void Enqueue(const std::wstring& line) {
    {
        std::lock_guard<std::mutex> lk(g_qmu);
        g_q.push_back(line);
    }
    // One posted message drains the whole queue; posting per line floods the
    // UI thread during token streaming and made the window feel stuck.
    if (g_hwnd && !g_linePosted.exchange(true))
        PostMessageW(g_hwnd, WM_AGENT_LINE, 0, 0);
}
static void SendCmd(const std::wstring& obj) {
    if (!g_stdinWr) return;
    std::string s = W8(obj + L"\n");
    DWORD n = 0;
    WriteFile(g_stdinWr, s.data(), (DWORD)s.size(), &n, NULL);
}

static void PushRecent(const std::wstring& p) {
    auto it = std::find(g_recents.begin(), g_recents.end(), p);
    if (it != g_recents.end()) g_recents.erase(it);
    g_recents.insert(g_recents.begin(), p);
    while (g_recents.size() > 7) g_recents.pop_back();
}

static void SaveSettings() {
    CreateDirectoryW((g_root + L"\\config").c_str(), NULL);
    std::ofstream f(W8(g_root + L"\\config\\settings.json"), std::ios::binary | std::ios::trunc);
    f << "{\"workspace\": \"" << W8(JEsc(g_wsPath)) << "\", \"model\": \"" << W8(JEsc(g_model))
      << "\", \"study\": " << (g_study ? "true" : "false") << "}";
}

static void LoadUiSettings() {
    std::ifstream f(W8(g_root + L"\\config\\settings.json"), std::ios::binary);
    if (!f) return;
    std::string s((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
    if (s.empty()) return;
    JV v = ParseJson(U8(s));
    g_study = v.flag(L"study");
}

// ---------------------------------------------------------------------------
// agent process
// ---------------------------------------------------------------------------
static std::wstring ExeDir() {
    wchar_t buf[MAX_PATH]; GetModuleFileNameW(NULL, buf, MAX_PATH);
    std::wstring p(buf);
    size_t sl = p.find_last_of(L"\\/"); if (sl != std::wstring::npos) p = p.substr(0, sl);
    sl = p.find_last_of(L"\\/");
    if (sl != std::wstring::npos && _wcsicmp(p.substr(sl + 1).c_str(), L"bin") == 0)
        return p.substr(0, sl);
    return p;
}

static HICON LoadAppIcon(int size) {
    wchar_t buf[MAX_PATH]; GetModuleFileNameW(NULL, buf, MAX_PATH);
    std::wstring p(buf);
    size_t sl = p.find_last_of(L"\\/");
    p = (sl == std::wstring::npos) ? std::wstring() : p.substr(0, sl + 1);
    HICON ic = (HICON)LoadImageW(NULL, (p + L"anvil.ico").c_str(), IMAGE_ICON,
                                 size, size, LR_LOADFROMFILE);
    if (!ic) ic = (HICON)LoadIconW(NULL, IDI_APPLICATION);
    return ic;
}

// ---------------------------------------------------------------------------
// clipboard, toast, tray
// ---------------------------------------------------------------------------
static bool CopyToClipboard(const std::wstring& s) {
    if (!OpenClipboard(g_hwnd)) return false;
    EmptyClipboard();
    size_t bytes = (s.size() + 1) * sizeof(wchar_t);
    HGLOBAL h = GlobalAlloc(GMEM_MOVEABLE, bytes);
    if (!h) { CloseClipboard(); return false; }
    void* p = GlobalLock(h);
    if (!p) { GlobalUnlock(h); GlobalFree(h); CloseClipboard(); return false; }
    memcpy(p, s.c_str(), bytes);
    GlobalUnlock(h);
    if (!SetClipboardData(CF_UNICODETEXT, h)) {
        GlobalFree(h); CloseClipboard(); return false;
    }
    CloseClipboard();
    return true;
}

static void ShowToast(const std::wstring& msg) {
    g_toast = msg;
    g_toastUntil = GetTickCount64() + 1900;
    if (g_hwnd) InvalidateRect(g_hwnd, NULL, FALSE);
}

static void SetStr(wchar_t* dst, size_t cap, const wchar_t* src) {
    if (!cap) return;
    wcsncpy(dst, src, cap - 1);
    dst[cap - 1] = 0;
}

static void TrayBalloon(const wchar_t* title, const wchar_t* body) {
    if (!g_trayAdded) return;
    SetStr(g_nid.szInfoTitle, 64, title);
    SetStr(g_nid.szInfo, 256, body);
    g_nid.dwInfoFlags = NIIF_INFO;
    g_nid.uFlags = NIF_INFO;
    Shell_NotifyIconW(NIM_MODIFY, &g_nid);
    g_nid.uFlags = NIF_ICON | NIF_MESSAGE | NIF_TIP;
}

static void AddTrayIcon(HWND hwnd) {
    g_nid.cbSize = sizeof(g_nid);
    g_nid.hWnd = hwnd;
    g_nid.uID = 1;
    g_nid.uFlags = NIF_ICON | NIF_MESSAGE | NIF_TIP;
    g_nid.uCallbackMessage = WM_TRAYICON;
    g_nid.hIcon = LoadAppIcon(32);
    SetStr(g_nid.szTip, 128, L"Anvil Fletch \x2014 local agent");
    g_trayAdded = Shell_NotifyIconW(NIM_ADD, &g_nid) != FALSE;
}

static void HideToTray(HWND hwnd) {
    ShowWindow(hwnd, SW_HIDE);
    TrayBalloon(L"Anvil Fletch is still running",
                L"The agent keeps working. Click the tray icon to reopen, right-click to stop or quit.");
}

static void ShowFromTray(HWND hwnd) {
    ShowWindow(hwnd, SW_SHOW);
    ShowWindow(hwnd, SW_RESTORE);
    SetForegroundWindow(hwnd);
    if (g_edit) SetFocus(g_edit);
}

static void AgentThread() {
    HANDLE rd = NULL, wr = NULL;
    SECURITY_ATTRIBUTES sa{ sizeof(sa), NULL, TRUE };
    if (!CreatePipe(&rd, &wr, &sa, 65536)) { g_dead = true; return; }
    SetHandleInformation(rd, HANDLE_FLAG_INHERIT, 0);

    std::wstring py = L"py";
    std::wstring args = L"src\\agent_backend.py --serve";
    STARTUPINFOW si{}; si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW;
    si.hStdOutput = wr; si.hStdError = wr; si.hStdInput = NULL;
    si.wShowWindow = SW_HIDE;

    HANDLE cinRd = NULL, cinWr = NULL;
    if (!CreatePipe(&cinRd, &cinWr, &sa, 65536)) { CloseHandle(rd); CloseHandle(wr); g_dead = true; return; }
    SetHandleInformation(cinWr, HANDLE_FLAG_INHERIT, 0);
    si.hStdInput = cinRd;
    g_stdinWr = cinWr;

    PROCESS_INFORMATION pi{};
    wchar_t resolved[MAX_PATH]{};
    if (SearchPathW(NULL, L"py", L".exe", MAX_PATH, resolved, NULL)) py = resolved;
    std::wstring line = L"\"" + py + L"\" " + args;
    std::vector<wchar_t> cmdbuf(line.begin(), line.end()); cmdbuf.push_back(0);

    BOOL ok = CreateProcessW(NULL, cmdbuf.data(), NULL, NULL, TRUE,
                             CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
                             NULL, g_root.c_str(), &si, &pi);
    CloseHandle(wr);
    if (!ok) {
        CloseHandle(rd); CloseHandle(cinRd); g_stdinWr = NULL; g_dead = true;
        Enqueue(L"{\"e\":\"fatal\",\"text\":\"Could not start Python. Is `py` on PATH?\"}");
        return;
    }
    g_proc = pi.hProcess;
    CloseHandle(pi.hThread);
    CloseHandle(cinRd);

    std::string acc;
    char tmp[4096]; DWORD n = 0;
    while (ReadFile(rd, tmp, sizeof(tmp), &n, NULL) && n > 0) {
        acc.append(tmp, tmp + n);
        size_t p;
        while ((p = acc.find('\n')) != std::string::npos) {
            std::string l = acc.substr(0, p); acc.erase(0, p + 1);
            if (!l.empty() && l.back() == '\r') l.pop_back();
            if (!l.empty()) Enqueue(U8(l));
        }
        if (acc.size() > 8u << 20) acc.clear();
    }
    if (!acc.empty()) Enqueue(U8(acc));
    WaitForSingleObject(pi.hProcess, 3000);
    CloseHandle(pi.hProcess); CloseHandle(rd); CloseHandle(g_stdinWr);
    g_stdinWr = NULL; g_proc = NULL; g_dead = true;
    Enqueue(L"{\"e\":\"exit\"}");
}

static void Boot() {
    if (g_booting) return;
    g_booting = true; g_dead = false;
    std::thread([]() { AgentThread(); g_booting = false; }).detach();
    std::thread([]() {
        for (int i = 0; i < 200 && !g_stdinWr; ++i) Sleep(25);
        if (!g_stdinWr) return;
        Sleep(200);
        SendCmd(L"{\"action\":\"ping\"}");
    }).detach();
}

// ---------------------------------------------------------------------------
// event handling
// ---------------------------------------------------------------------------
static void OnEvent(const JV& v) {
    std::wstring e = v.str(L"e");
    ++g_uiRev;

    if (e == L"ready") {
        std::wstring w = v.str(L"workspace"); if (!w.empty()) g_wsPath = w;
        std::wstring m = v.str(L"model"); if (!m.empty()) g_model = m;
        g_toolCount = (int)v.num(L"tools");
        g_lessonCount = (int)v.num(L"lessons");
        if (!g_pending.empty()) {
            std::wstring p = g_pending; bool st = g_pendingStudy;
            g_pending.clear(); g_pendingStudy = false;
            Dispatch(p, st);
            g_attachments.clear();
        }
        SendCmd(L"{\"action\":\"recents\"}");
        return;
    }
    if (e == L"recents") {
        if (auto* lst = v.list(L"items")) {
            g_recents.clear();
            for (auto& it : *lst)
                if (it.t == JV::Str && !it.s.empty()) g_recents.push_back(it.s);
        }
        return;
    }
    if (e == L"lessons") {
        g_lessonCount = (int)v.num(L"lessons");
        return;
    }
    if (e == L"probe") {
        g_online = v.flag(L"online");
        if (auto* lst = v.list(L"models")) {
            g_models.clear();
            for (auto& m : *lst) if (m.t == JV::Str) g_models.push_back(m.s);
        }
        return;
    }
    if (e == L"models") {
        if (auto* lst = v.list(L"items")) {
            std::vector<std::wstring> next;
            for (auto& m : *lst) if (m.t == JV::Str) next.push_back(m.s);
            if (!next.empty()) g_models = next;
        }
        return;
    }
    if (e == L"fatal" || e == L"exit") {
        g_busy = false; g_dead = true; g_status = L"offline";
        if (g_study) { g_study = false; g_studyStartPending = false; SaveSettings(); }
        if (!g_turns.empty() && g_turns.back().running) {
            Turn& t = g_turns.back();
            t.running = false;
            t.answer = L"The agent process stopped. It restarts automatically on your next message.";
            t.answerDirty = true;
        }
        return;
    }

    if (g_turns.empty() || !g_turns.back().running) {
        if (e == L"status") {
            g_status = v.str(L"text");
            std::wstring st = v.str(L"state");
            // "busy" can arrive from a stale request the backend rejected; with
            // no turn on screen there is nothing to wait for, so never latch.
            if (st == L"thinking" || st == L"warming" || st == L"writing" || st == L"stopping")
                g_busy = true;
            else
                g_busy = false;
        } else if (e == L"done") {
            g_busy = false; g_status = L"ready";
            g_prog.active = false;
        }
        return;
    }
    Turn& t = g_turns.back();

    if (e == L"progress") {
        g_prog.label = v.str(L"label");
        g_prog.item = v.str(L"item");
        g_prog.done = (long long)v.num(L"done");
        g_prog.total = (long long)v.num(L"total");
        g_prog.filesDone = (long long)v.num(L"files_done");
        g_prog.filesTotal = (long long)v.num(L"files_total");
        g_prog.active = true;
        g_prog.until = GetTickCount64() + 4000;
        return;
    }
    if (e == L"status") {
        g_status = v.str(L"text");
        std::wstring st = v.str(L"state");
        if (st == L"thinking" || st == L"warming" || st == L"writing" || st == L"stopping") g_busy = true;
        if (st == L"done" || st == L"error" || st == L"ready") g_busy = false;
    } else if (e == L"thinking") {
        t.liveThought = Trim(v.str(L"text"));
    } else if (e == L"token") {
        // Streaming chunks arrive many times a second. Mark the text only; the
        // painter re-wraps it on its own short clock instead of once per token.
        t.answer += v.str(L"text");
    } else if (e == L"answer") {
        t.answer = v.str(L"text");
        t.answerDirty = true;
    } else if (e == L"thought") {
        t.liveThought.clear();
        Step s; s.kind = Step::Thought; s.text = v.str(L"text");
        t.steps.push_back(s);
    } else if (e == L"tool") {
        std::wstring op = v.str(L"op");
        if (op == L"start") {
            t.liveThought.clear();
            Step s; s.kind = Step::Tool; s.running = true;
            s.name = v.str(L"name"); s.text = v.str(L"label");
            if (const JV* a = v.at(L"args"))
                for (auto& kv : a->obj) s.args.push_back({kv.first, kv.second.s});
            t.steps.push_back(s);
            t.tools++;
        } else {
            for (auto it = t.steps.rbegin(); it != t.steps.rend(); ++it) {
                if (it->kind == Step::Tool && it->running && IEquals(it->name, v.str(L"name"))) {
                    it->running = false;
                    it->ok = v.flag(L"ok");
                    it->summary = v.str(L"summary");
                    it->detail = v.str(L"detail");
                    it->ms = (long long)v.num(L"ms");
                    std::wstring sum = v.str(L"summary");
                    it->stopped = !it->ok && sum.rfind(L"Stopped by you", 0) == 0;
                    if (!it->ok && !it->stopped) t.fails++;
                    break;
                }
            }
        }
    } else if (e == L"plan") {
        Step s; s.kind = Step::Plan;
        if (auto* lst = v.list(L"items"))
            for (auto& it : *lst) s.items.push_back({it.str(L"text"), it.str(L"status", L"pending")});
        t.steps.push_back(s);
    } else if (e == L"compact" || e == L"log") {
        Step s; s.kind = Step::Note; s.text = v.str(L"text");
        t.steps.push_back(s);
    } else if (e == L"done") {
        t.running = false;
        t.ms = NowMs() - t.started;
        t.liveThought.clear();
        g_prog.active = false;
        for (auto& s : t.steps) s.running = false;
        if (t.answer.empty()) { t.answer = L"(no reply)"; t.answerDirty = true; }
        bool wasStudy = t.studySession;
        g_busy = false; g_status = L"ready";
        if (g_hwnd && !IsWindowVisible(g_hwnd) && !g_study)
            TrayBalloon(L"Anvil Fletch finished",
                        L"The run is done. Click the tray icon to review the result.");
        if (wasStudy && g_study) { g_study = false; SaveSettings(); }
        if (g_studyStartPending) {
            g_studyStartPending = false;
            PostMessageW(g_hwnd, WM_STUDY_START, 0, 0);
        }
    }
}

// A running tool or active plan row draws an animated spinner, which is the
// only thing on screen that changes without a new event arriving.
static bool LiveSpinner() {
    if (g_turns.empty()) return false;
    const Turn& t = g_turns.back();
    if (!t.running) return false;
    // The header "Replying" arc is drawn even before the first tool step, and
    // it is the only visible motion during slow model turns.
    if (!g_spinners.empty()) return true;
    for (const auto& s : t.steps) {
        if (s.kind == Step::Tool && s.running) return true;
        if (s.kind == Step::Plan)
            for (const auto& it : s.items)
                if (it.second == L"active") return true;
    }
    return false;
}

// ---------------------------------------------------------------------------
// geometry
// ---------------------------------------------------------------------------
static RECT ClientRect() { RECT rc; GetClientRect(g_hwnd, &rc); return rc; }
static int ColW() {
    RECT rc = ClientRect();
    int avail = rc.right - SIDE_W;
    int w = (std::min)(avail - 96, 900);
    if (w < 340) w = (std::max)(280, avail - 40);
    return w;
}
static int ColX() {
    RECT rc = ClientRect();
    return SIDE_W + (rc.right - SIDE_W - ColW()) / 2;
}
static RECT SideRect() { RECT rc = ClientRect(); return { 0, 0, SIDE_W, rc.bottom }; }
static RECT HeadRect() { RECT rc = ClientRect(); return { SIDE_W, 0, rc.right, HEAD_H }; }
static RECT CompRect() {
    RECT rc = ClientRect();
    int x = ColX(), w = ColW();
    int bottom = rc.bottom - 14;
    return { x, bottom - g_compH, x + w, bottom };
}
static RECT EditRect() {
    RECT c = CompRect();
    return { c.left + 18, c.top + 14, c.right - 116, c.bottom - 42 };
}
static RECT ViewRect() {
    RECT rc = ClientRect();
    return { SIDE_W, HEAD_H, rc.right, CompRect().top - 6 };
}

// ---------------------------------------------------------------------------
// GDI+ shape helpers
// ---------------------------------------------------------------------------
static GraphicsPath* RoundPath(float x, float y, float w, float h, float r) {
    r = (std::min)(r, (std::min)(w, h) / 2.0f);
    if (r < 0.5f) {
        GraphicsPath* p = new GraphicsPath();
        p->AddRectangle(RectF(x, y, w, h));
        return p;
    }
    float d = r * 2;
    GraphicsPath* p = new GraphicsPath();
    p->AddArc(x, y, d, d, 180, 90);
    p->AddArc(x + w - d, y, d, d, 270, 90);
    p->AddArc(x + w - d, y + h - d, d, d, 0, 90);
    p->AddArc(x, y + h - d, d, d, 90, 90);
    p->CloseFigure();
    return p;
}
static void FillRound(Graphics& g, RECT rc, COLORREF c, float r = 8) {
    SolidBrush br(Color(255, GetRValue(c), GetGValue(c), GetBValue(c)));
    GraphicsPath* p = RoundPath((float)rc.left, (float)rc.top,
                                (float)(rc.right - rc.left), (float)(rc.bottom - rc.top), r);
    g.FillPath(&br, p); delete p;
}
static void FillRoundA(Graphics& g, RECT rc, COLORREF c, BYTE a, float r = 8) {
    SolidBrush br(Color(a, GetRValue(c), GetGValue(c), GetBValue(c)));
    GraphicsPath* p = RoundPath((float)rc.left, (float)rc.top,
                                (float)(rc.right - rc.left), (float)(rc.bottom - rc.top), r);
    g.FillPath(&br, p); delete p;
}
static void FillRoundGrad(Graphics& g, RECT rc, COLORREF a, COLORREF b, float r,
                          bool horizontal = true) {
    LinearGradientBrush br(
        RectF((REAL)rc.left, (REAL)rc.top, (REAL)(rc.right - rc.left), (REAL)(rc.bottom - rc.top)),
        Color(255, GetRValue(a), GetGValue(a), GetBValue(a)),
        Color(255, GetRValue(b), GetGValue(b), GetBValue(b)),
        horizontal ? LinearGradientModeHorizontal : LinearGradientModeVertical);
    GraphicsPath* p = RoundPath((float)rc.left, (float)rc.top,
                                (float)(rc.right - rc.left), (float)(rc.bottom - rc.top), r);
    g.FillPath(&br, p); delete p;
}
static void StrokeRound(Graphics& g, RECT rc, COLORREF c, float w = 1, float r = 8) {
    Pen pen(Color(255, GetRValue(c), GetGValue(c), GetBValue(c)), w);
    GraphicsPath* p = RoundPath((float)rc.left, (float)rc.top,
                                (float)(rc.right - rc.left), (float)(rc.bottom - rc.top), r);
    g.DrawPath(&pen, p); delete p;
}
static void StrokeRoundA(Graphics& g, RECT rc, COLORREF c, BYTE a, float w = 1, float r = 8) {
    Pen pen(Color(a, GetRValue(c), GetGValue(c), GetBValue(c)), w);
    GraphicsPath* p = RoundPath((float)rc.left, (float)rc.top,
                                (float)(rc.right - rc.left), (float)(rc.bottom - rc.top), r);
    g.DrawPath(&pen, p); delete p;
}
static void FillRectC(Graphics& g, RECT rc, COLORREF c) {
    SolidBrush br(Color(255, GetRValue(c), GetGValue(c), GetBValue(c)));
    g.FillRectangle(&br, (REAL)rc.left, (REAL)rc.top,
                    (REAL)(rc.right - rc.left), (REAL)(rc.bottom - rc.top));
}
static void LineC(Graphics& g, int x1, int y1, int x2, int y2, COLORREF c, float w = 1) {
    Pen pen(Color(255, GetRValue(c), GetGValue(c), GetBValue(c)), w);
    g.DrawLine(&pen, (REAL)x1, (REAL)y1, (REAL)x2, (REAL)y2);
}
static void EllipseC(Graphics& g, int cx, int cy, float r, COLORREF c, bool fill = true) {
    SolidBrush br(Color(255, GetRValue(c), GetGValue(c), GetBValue(c)));
    Pen pen(Color(255, GetRValue(c), GetGValue(c), GetBValue(c)), 1.6f);
    if (fill) g.FillEllipse(&br, (REAL)(cx - r), (REAL)(cy - r), (REAL)(r * 2), (REAL)(r * 2));
    else g.DrawEllipse(&pen, (REAL)(cx - r), (REAL)(cy - r), (REAL)(r * 2), (REAL)(r * 2));
}

// icons --------------------------------------------------------------------
static void IconCheck(Graphics& g, float cx, float cy, COLORREF c, float s = 1.0f) {
    Pen pen(Color(255, GetRValue(c), GetGValue(c), GetBValue(c)), 1.9f * s);
    pen.SetStartCap(LineCapRound); pen.SetEndCap(LineCapRound);
    PointF pts[3] = {
        PointF(cx - 4 * s, cy + 0.5f * s),
        PointF(cx - 1 * s, cy + 3.5f * s),
        PointF(cx + 4.5f * s, cy - 3.5f * s) };
    g.DrawLines(&pen, pts, 3);
}
static void IconX(Graphics& g, float cx, float cy, COLORREF c, float s = 1.0f) {
    Pen pen(Color(255, GetRValue(c), GetGValue(c), GetBValue(c)), 1.9f * s);
    pen.SetStartCap(LineCapRound); pen.SetEndCap(LineCapRound);
    g.DrawLine(&pen, cx - 4 * s, cy - 4 * s, cx + 4 * s, cy + 4 * s);
    g.DrawLine(&pen, cx + 4 * s, cy - 4 * s, cx - 4 * s, cy + 4 * s);
}
static void PaintSpinnerAt(Graphics& g, float cx, float cy, COLORREF c, float s) {
    long long phase = (NowMs() % 900) * 360 / 900;
    for (int i = 0; i < 3; ++i) {
        Color ac((BYTE)(235 - i * 72), GetRValue(c), GetGValue(c), GetBValue(c));
        Pen sp(ac, 2.0f * s); sp.SetStartCap(LineCapRound);
        float a0 = (float)((phase + i * 40) % 360);
        g.DrawArc(&sp, cx - 6 * s, cy - 6 * s, 12 * s, 12 * s, a0, 46.0f);
    }
}
static void IconSpinner(Graphics& g, float cx, float cy, COLORREF c, float s = 1.0f) {
    if (g_recordSpinners) {
        g_spinners.push_back({ cx, cy, c, s });
        return;
    }
    PaintSpinnerAt(g, cx, cy, c, s);
}
static void IconChevron(Graphics& g, float cx, float cy, bool down, COLORREF c, float s = 1.0f) {
    Pen pen(Color(255, GetRValue(c), GetGValue(c), GetBValue(c)), 1.7f * s);
    pen.SetStartCap(LineCapRound); pen.SetEndCap(LineCapRound);
    PointF pts[3];
    if (down) {
        pts[0] = PointF(cx - 3.5f * s, cy - 2 * s);
        pts[1] = PointF(cx, cy + 2 * s);
        pts[2] = PointF(cx + 3.5f * s, cy - 2 * s);
    } else {
        pts[0] = PointF(cx - 2 * s, cy - 3.5f * s);
        pts[1] = PointF(cx + 2 * s, cy);
        pts[2] = PointF(cx - 2 * s, cy + 3.5f * s);
    }
    g.DrawLines(&pen, pts, 3);
}
static void IconFolder(Graphics& g, float cx, float cy, COLORREF c, float s = 1.0f) {
    Pen pen(Color(255, GetRValue(c), GetGValue(c), GetBValue(c)), 1.5f * s);
    pen.SetLineJoin(LineJoinRound);
    g.DrawRectangle(&pen, cx - 7 * s, cy - 4 * s, 14 * s, 10 * s);
    g.DrawRectangle(&pen, cx - 7 * s, cy - 6.5f * s, 6 * s, 3 * s);
}
static void IconChip(Graphics& g, float cx, float cy, COLORREF c, float s = 1.0f) {
    Pen pen(Color(255, GetRValue(c), GetGValue(c), GetBValue(c)), 1.5f * s);
    g.DrawRectangle(&pen, cx - 6 * s, cy - 6 * s, 12 * s, 12 * s);
    g.DrawRectangle(&pen, cx - 3 * s, cy - 3 * s, 6 * s, 6 * s);
}
static void IconLayers(Graphics& g, float cx, float cy, COLORREF c, float s = 1.0f) {
    Pen pen(Color(255, GetRValue(c), GetGValue(c), GetBValue(c)), 1.5f * s);
    pen.SetLineJoin(LineJoinRound);
    PointF a[4] = { PointF(cx, cy - 6 * s), PointF(cx + 7 * s, cy - 2 * s),
                    PointF(cx, cy + 2 * s), PointF(cx - 7 * s, cy - 2 * s) };
    g.DrawPolygon(&pen, a, 4);
    g.DrawLine(&pen, cx - 7 * s, cy + 1 * s, cx, cy + 5 * s);
    g.DrawLine(&pen, cx + 7 * s, cy + 1 * s, cx, cy + 5 * s);
}
static void IconPen(Graphics& g, float cx, float cy, COLORREF c, float s = 1.0f) {
    Pen pen(Color(255, GetRValue(c), GetGValue(c), GetBValue(c)), 1.6f * s);
    pen.SetStartCap(LineCapRound); pen.SetEndCap(LineCapRound);
    g.DrawLine(&pen, cx - 5 * s, cy + 5 * s, cx + 5 * s, cy - 5 * s);
    g.DrawLine(&pen, cx + 2 * s, cy - 7 * s, cx + 7 * s, cy - 2 * s);
    g.DrawLine(&pen, cx - 6 * s, cy + 6 * s, cx - 2.5f * s, cy + 6 * s);
}
static void IconDownload(Graphics& g, float cx, float cy, COLORREF c, float s = 1.0f) {
    Pen pen(Color(255, GetRValue(c), GetGValue(c), GetBValue(c)), 1.6f * s);
    pen.SetStartCap(LineCapRound); pen.SetEndCap(LineCapRound);
    g.DrawLine(&pen, cx, cy - 7 * s, cx, cy + 2.5f * s);
    g.DrawLine(&pen, cx - 3.6f * s, cy - 1 * s, cx, cy + 2.8f * s);
    g.DrawLine(&pen, cx + 3.6f * s, cy - 1 * s, cx, cy + 2.8f * s);
    g.DrawLine(&pen, cx - 6.5f * s, cy + 6.5f * s, cx + 6.5f * s, cy + 6.5f * s);
}
static void IconGlobe(Graphics& g, float cx, float cy, COLORREF c, float s = 1.0f) {
    Pen pen(Color(255, GetRValue(c), GetGValue(c), GetBValue(c)), 1.4f * s);
    g.DrawEllipse(&pen, cx - 6.5f * s, cy - 6.5f * s, 13 * s, 13 * s);
    g.DrawLine(&pen, cx - 6.5f * s, cy, cx + 6.5f * s, cy);
    g.DrawBezier(&pen, cx, cy - 6.5f * s, cx - 4 * s, cy, cx - 4 * s, cy, cx, cy + 6.5f * s);
    g.DrawBezier(&pen, cx, cy - 6.5f * s, cx + 4 * s, cy, cx + 4 * s, cy, cx, cy + 6.5f * s);
}
static void IconClock(Graphics& g, float cx, float cy, COLORREF c, float s = 1.0f) {
    Pen pen(Color(255, GetRValue(c), GetGValue(c), GetBValue(c)), 1.4f * s);
    g.DrawEllipse(&pen, cx - 6 * s, cy - 6 * s, 12 * s, 12 * s);
    g.DrawLine(&pen, cx, cy, cx, cy - 3.5f * s);
    g.DrawLine(&pen, cx, cy, cx + 3 * s, cy + 1 * s);
}
static void IconSpark(Graphics& g, float cx, float cy, COLORREF c, float s = 1.0f) {
    for (int i = 0; i < 4; ++i) {
        float a = 0.7854f * i + 0.3927f;
        float r0 = 2.2f * s, r1 = 6.5f * s;
        Pen pen(Color(255, GetRValue(c), GetGValue(c), GetBValue(c)), 1.5f * s);
        pen.SetStartCap(LineCapRound); pen.SetEndCap(LineCapRound);
        g.DrawLine(&pen, cx + cos(a) * r0, cy + sin(a) * r0, cx + cos(a) * r1, cy + sin(a) * r1);
        a += 3.14159f;
        g.DrawLine(&pen, cx + cos(a) * r0, cy + sin(a) * r0, cx + cos(a) * r1, cy + sin(a) * r1);
    }
}
static void IconSend(Graphics& g, float cx, float cy, COLORREF c) {
    Pen pen(Color(255, GetRValue(c), GetGValue(c), GetBValue(c)), 2.2f);
    pen.SetStartCap(LineCapRound); pen.SetEndCap(LineCapRound);
    g.DrawLine(&pen, cx, cy + 7, cx, cy - 6);
    PointF pts[3] = { PointF(cx - 5.5f, cy - 1.5f), PointF(cx, cy - 7.5f), PointF(cx + 5.5f, cy - 1.5f) };
    g.DrawLines(&pen, pts, 3);
}
static void IconStop(Graphics& g, float cx, float cy, COLORREF c) {
    SolidBrush br(Color(255, GetRValue(c), GetGValue(c), GetBValue(c)));
    GraphicsPath* p = RoundPath(cx - 6.5f, cy - 6.5f, 13.0f, 13.0f, 3.5f);
    g.FillPath(&br, p); delete p;
}
static void IconPlus(Graphics& g, float cx, float cy, COLORREF c, float s = 1.0f) {
    Pen pen(Color(255, GetRValue(c), GetGValue(c), GetBValue(c)), 2.0f * s);
    pen.SetStartCap(LineCapRound); pen.SetEndCap(LineCapRound);
    g.DrawLine(&pen, cx - 5.5f * s, cy, cx + 5.5f * s, cy);
    g.DrawLine(&pen, cx, cy - 5.5f * s, cx, cy + 5.5f * s);
}
static void IconCopy(Graphics& g, float cx, float cy, COLORREF c, float s = 1.0f) {
    Pen pen(Color(255, GetRValue(c), GetGValue(c), GetBValue(c)), 1.4f * s);
    pen.SetLineJoin(LineJoinRound);
    g.DrawRectangle(&pen, cx - 6.5f * s, cy - 6.5f * s, 8.5f * s, 10 * s);
    g.DrawRectangle(&pen, cx - 3.0f * s, cy - 3.0f * s, 8.5f * s, 10 * s);
}
static void IconAnvil(Graphics& g, float cx, float cy, float s, COLORREF body) {
    SolidBrush br(Color(255, GetRValue(body), GetGValue(body), GetBValue(body)));
    PointF bodyPts[9] = {
        PointF(cx - 10 * s, cy - 5 * s), PointF(cx + 8 * s, cy - 5 * s),
        PointF(cx + 13 * s, cy - 3 * s), PointF(cx + 9 * s, cy - 1.2f * s),
        PointF(cx + 6 * s, cy - 1.2f * s), PointF(cx + 5 * s, cy + 2 * s),
        PointF(cx - 5 * s, cy + 2 * s), PointF(cx - 6 * s, cy - 1.2f * s),
        PointF(cx - 10 * s, cy - 1.2f * s) };
    GraphicsPath p;
    p.AddPolygon(bodyPts, 9);
    g.FillPath(&br, &p);
    g.FillRectangle(&br, cx - 3.5f * s, cy + 2 * s, 7 * s, 2.2f * s);
    GraphicsPath base;
    base.AddRectangle(RectF(cx - 7.5f * s, cy + 4.2f * s, 15 * s, 3.4f * s));
    g.FillPath(&br, &base);
}

// GDI text drawn between GDI+ operations forces the Graphics context to
// re-lock the surface every time, which made a full frame take ~100ms. Text is
// queued during painting and rendered in one GDI pass per layer instead.
struct TextOp {
    int x, y;
    std::wstring s;
    HFONT f;
    COLORREF c;
    RECT clip;
};
static std::vector<TextOp> g_textOps;
static bool g_textCollect = false;
static std::map<HFONT, Gdiplus::Font*> g_gpFonts;

static Gdiplus::Font* FontFor(HDC hdc, HFONT f) {
    auto it = g_gpFonts.find(f);
    if (it != g_gpFonts.end()) return it->second;
    LOGFONTW lf{};
    Gdiplus::Font* nf = nullptr;
    if (GetObjectW(f, sizeof(lf), &lf)) nf = new Gdiplus::Font(hdc, &lf);
    if (!nf || nf->GetLastStatus() != Gdiplus::Ok) {
        delete nf;
        nf = new Gdiplus::Font(L"Segoe UI Variable Text", 14.0f, Gdiplus::FontStyleRegular,
                               Gdiplus::UnitPixel);
    }
    g_gpFonts[f] = nf;
    return nf;
}

static void TextAt(HDC hdc, int x, int y, const std::wstring& s, HFONT f, COLORREF c) {
    if (g_textCollect) {
        TextOp op;
        op.x = x; op.y = y; op.s = s; op.f = f; op.c = c;
        GetClipBox(hdc, &op.clip);
        g_textOps.push_back(std::move(op));
        return;
    }
    HFONT old = (HFONT)SelectObject(hdc, f);
    SetTextColor(hdc, c);
    SetBkMode(hdc, TRANSPARENT);
    TextOutW(hdc, x, y, s.c_str(), (int)s.size());
    SelectObject(hdc, old);
}

static void FlushTextOps(HDC hdc, const RECT& full) {
    g_textCollect = false;
    if (g_textOps.empty()) return;
    Graphics g(hdc);
    g.SetTextRenderingHint(TextRenderingHintClearTypeGridFit);
    g.SetPixelOffsetMode(PixelOffsetModeHalf);
    // Typographic metrics keep DrawString advances in line with the GDI
    // measurements used for ellipsizing; default StringFormat adds side bearing.
    StringFormat fmt(StringFormatFlagsNoWrap | StringFormatFlagsNoClip |
                     StringFormatFlagsMeasureTrailingSpaces);
    fmt.SetFormatFlags(fmt.GetFormatFlags() | StringFormatFlagsNoFitBlackBox);
    for (const TextOp& op : g_textOps) {
        bool clipped = op.clip.left > full.left || op.clip.top > full.top ||
                       op.clip.right < full.right || op.clip.bottom < full.bottom;
        if (clipped)
            g.SetClip(Rect(op.clip.left, op.clip.top,
                           op.clip.right - op.clip.left, op.clip.bottom - op.clip.top));
        SolidBrush br(Color(255, GetRValue(op.c), GetGValue(op.c), GetBValue(op.c)));
        g.DrawString(op.s.c_str(), (INT)op.s.size(), FontFor(hdc, op.f),
                     PointF((REAL)op.x, (REAL)op.y), &fmt, &br);
        if (clipped) g.ResetClip();
    }
    g_textOps.clear();
}
static int TextW(HDC hdc, const std::wstring& s, HFONT f) {
    HFONT old = (HFONT)SelectObject(hdc, f);
    SIZE sz{}; GetTextExtentPoint32W(hdc, s.c_str(), (int)s.size(), &sz);
    SelectObject(hdc, old);
    return sz.cx;
}

// ---------------------------------------------------------------------------
// transcript layout
// ---------------------------------------------------------------------------
struct Hit { RECT rc; int id; };
static std::vector<Hit> g_hits;
static std::vector<Hit> g_hitsSide;

static int DetailLines(const std::wstring& d, int cap) {
    if (d.empty()) return 0;
    int lines = 0; size_t p = 0;
    while (p <= d.size() && lines < cap) {
        size_t nl = d.find(L'\n', p);
        p = (nl == std::wstring::npos) ? d.size() + 1 : nl + 1; ++lines;
    }
    return lines;
}

static int StepHeight(HDC hdc, const Step& s) {
    if (s.kind == Step::Tool) {
        int h = 30;
        if (s.expanded) {
            h += 6 + (int)s.args.size() * 17;
            std::wstring d = s.detail.empty() ? s.summary : s.detail;
            int lines = DetailLines(d, 16);
            if (lines) h += 10 + lines * LH_MONO + 12;
        }
        return h;
    }
    if (s.kind == Step::Plan) return 36 + (int)s.items.size() * 28 + 8;
    if (s.kind == Step::Note) return 23;
    return 26;
}

static void PaintStep(HDC hdc, Graphics& g, const Step& s, int x0, int y, int w,
                      int index, int turnIndex) {
    if (s.kind == Step::Thought) {
        EllipseC(g, x0 + 13, y + 12, 7, C_SHEET);
        IconSpark(g, (float)x0 + 13, (float)y + 12, C_THINK, 0.95f);
        TextAt(hdc, x0 + 28, y + 5, L"thinking", F_SMALLB, C_THINK);
        TextAt(hdc, x0 + 88, y + 5,
               Ellipsize(hdc, s.text, w - 100, F_SMALL), F_SMALL, C_DIM);
        return;
    }
    if (s.kind == Step::Note) {
        EllipseC(g, x0 + 13, y + 11, 2.4f, C_DIM);
        TextAt(hdc, x0 + 28, y + 4, Ellipsize(hdc, s.text, w - 40, F_SMALL), F_SMALL, C_DIM);
        return;
    }
    if (s.kind == Step::Plan) {
        int h = StepHeight(hdc, s);
        RECT card{ x0, y, x0 + (std::min)(w, 520), y + h };
        FillRound(g, card, C_PANEL, 10);
        StrokeRound(g, card, C_LINE, 1, 10);
        TextAt(hdc, card.left + 14, card.top + 8, L"PLAN", F_LABEL, C_ACCENT);
        int iy = card.top + 28;
        for (auto& it : s.items) {
            float cx = (float)card.left + 22;
            float cy = (float)iy + 9;
            const std::wstring& st = it.second;
            RECT box{ (int)cx - 6, iy + 2, (int)cx + 6, iy + 14 };
            if (st == L"done") {
                FillRound(g, box, C_ACCENT, 4);
                IconCheck(g, cx, cy, C_INK, 0.72f);
            } else if (st == L"active") {
                StrokeRound(g, box, C_ACCENT, 1.5f, 4);
                IconSpinner(g, cx, cy, C_ACCENT, 0.7f);
            } else {
                StrokeRound(g, box, C_LINE2, 1.2f, 4);
            }
            COLORREF tc = (st == L"done") ? C_DIM : (st == L"active" ? C_TEXT : C_MUTED);
            HFONT pf = (st == L"active") ? F_UIB : F_UI;
            TextAt(hdc, card.left + 38, iy,
                   Ellipsize(hdc, it.first, card.right - card.left - 52, pf), pf, tc);
            iy += 28;
        }
        return;
    }

    // Tool row
    RECT row{ x0, y, x0 + w, y + 30 };
    int id = HOT_STEP_BASE + turnIndex * 100 + index;
    bool hot = (g_hover == id) || s.expanded;
    if (hot) FillRound(g, { row.left - 8, row.top, row.right - 4, row.bottom - 2 }, C_CARD, 7);

    float cx = (float)row.left + 13, cy = (float)row.top + 15;
    EllipseC(g, (int)cx, (int)cy, 9, C_SHEET);
    if (s.running) {
        StrokeRoundA(g, { (int)cx - 9, (int)cy - 9, (int)cx + 9, (int)cy + 9 }, C_ACCENT, 70, 1.4f, 9);
        IconSpinner(g, cx, cy, C_ACCENT, 0.85f);
    } else if (s.stopped) {
        StrokeRoundA(g, { (int)cx - 9, (int)cy - 9, (int)cx + 9, (int)cy + 9 }, C_ACCENT, 70, 1.2f, 9);
        RECT bar{ (int)cx - 3, (int)cy - 3, (int)cx + 3, (int)cy + 3 };
        FillRound(g, bar, C_ACCENT, 1.5f);
    } else if (s.ok) {
        StrokeRoundA(g, { (int)cx - 9, (int)cy - 9, (int)cx + 9, (int)cy + 9 }, C_OK, 55, 1.2f, 9);
        IconCheck(g, cx, cy, C_OK, 0.85f);
    } else {
        StrokeRoundA(g, { (int)cx - 9, (int)cy - 9, (int)cx + 9, (int)cy + 9 }, C_ERR, 70, 1.2f, 9);
        IconX(g, cx, cy, C_ERR, 0.78f);
    }

    int tx = row.left + 30;
    TextAt(hdc, tx, row.top + 6, s.name, F_MONOS, (s.ok || s.stopped) ? C_TEXT : C_ERR);
    tx += TextW(hdc, s.name, F_MONOS) + 10;
    if (!s.text.empty()) {
        int avail = row.right - tx - 96;
        if (avail < 60) avail = 60;
        TextAt(hdc, tx, row.top + 7, Ellipsize(hdc, s.text, avail, F_SMALL), F_SMALL, C_MUTED);
    }
    if (!s.running) {
        std::wstring dur = DurStr(s.ms);
        int dw = TextW(hdc, dur, F_SMALL);
        TextAt(hdc, row.right - 30 - dw, row.top + 7, dur, F_SMALL, C_DIM);
    }
    IconChevron(g, (float)row.right - 16, cy, s.expanded, C_DIM, 0.85f);

    if (!s.expanded) return;
    int yy = y + 34;
    for (auto& kv : s.args) {
        TextAt(hdc, row.left + 30, yy, kv.first, F_MONOS, C_DIM);
        int kw = TextW(hdc, kv.first, F_MONOS);
        std::wstring val = kv.second;
        std::wstring first = val.substr(0, val.find(L'\n'));
        TextAt(hdc, row.left + 30 + kw + 8, yy,
               Ellipsize(hdc, first, row.right - (row.left + 48 + kw), F_MONOS), F_MONOS, C_CODE);
        yy += 17;
    }
    std::wstring d = s.detail.empty() ? s.summary : s.detail;
    int lines = DetailLines(d, 16);
    if (!lines) return;
    yy += 6;
    int bh = lines * LH_MONO + 12;
    RECT box{ row.left + 30, yy, row.right - 10, yy + bh };
    FillRound(g, box, RGB(18, 16, 13), 8);
    StrokeRound(g, box, C_LINE, 1, 8);
    int ty = yy + 6;
    size_t p = 0;
    for (int i = 0; i < lines; ++i) {
        size_t nl = d.find(L'\n', p);
        std::wstring seg = (nl == std::wstring::npos) ? d.substr(p) : d.substr(p, nl - p);
        p = (nl == std::wstring::npos) ? d.size() + 1 : nl + 1;
        TextAt(hdc, box.left + 10, ty, Ellipsize(hdc, seg, box.right - box.left - 22, F_MONOS),
               F_MONOS, (s.ok || s.stopped) ? C_MUTED : RGB(235, 156, 146));
        ty += LH_MONO;
    }
}

static bool ShowSteps(const Turn& t) { return t.running || t.expanded; }

static int MeasureTurn(HDC hdc, Turn& t, int w) {
    int h = 14;
    if (!t.user.empty()) {
        if (t.userW != w || t.userLines.empty()) {
            std::vector<Run> runs; SplitRuns(t.user, C_TEXT, F_CHAT, runs);
            t.userLines.clear();
            WrapRuns(hdc, runs, (std::min)(w - 64, 570), 0, 0, t.userLines);
            t.userW = w;
        }
        h += (int)t.userLines.size() * LH_CHAT + 26;
        h += 24;
    }
    h += 26;  // assistant identity row
    if (!t.steps.empty() && ShowSteps(t)) {
        for (auto& s : t.steps) h += StepHeight(hdc, s) + 2;
        h += 6;
    }
    if (!t.running && !t.steps.empty() && !t.expanded) h += 36;
    if (!t.answer.empty()) {
        // While streaming, re-wrap at most ~8 times a second: markdown layout
        // is the expensive part and redoing it per token stalls the UI thread.
        bool streamTick = t.running && t.answer.size() != t.answerRenderedLen &&
                          (NowMs() - t.answerRenderedAt >= 120);
        if (t.answerDirty || t.answerW != w || streamTick) {
            RenderMarkdown(hdc, t.answer, w, t.answerLines);
            t.answerDirty = false; t.answerW = w;
            t.answerRenderedAt = NowMs();
            t.answerRenderedLen = t.answer.size();
        }
        for (auto& l : t.answerLines) h += l.code ? LH_MONO : LH_CHAT;
        h += 14;
    }
    if (t.running) h += 28;
    return h + 22;
}

static int PaintTurn(HDC hdc, Graphics& g, Turn& t, int x0, int y, int w, int ti) {
    int startY = y;
    if (!t.user.empty()) {
        int widest = 0;
        for (auto& l : t.userLines) widest = (std::max)(widest, l.w);
        int bw = (std::min)((std::min)(w - 20, 620), (std::max)(widest + 52, 130));
        int bh = (int)t.userLines.size() * LH_CHAT + 26;
        RECT bub{ x0 + w - bw, y, x0 + w, y + bh };
        FillRound(g, bub, C_USERBG, 12);
        StrokeRound(g, bub, C_USERBRD, 1, 12);
        RECT cp{ bub.right - 26, bub.top + 7, bub.right - 6, bub.top + 27 };
        bool cpHot = g_hover == HOT_COPY_TURN_BASE + ti;
        if (cpHot) FillRound(g, cp, C_CARD_H, 6);
        IconCopy(g, (float)(cp.left + cp.right) / 2, (float)(cp.top + cp.bottom) / 2,
                 cpHot ? C_TEXT : C_DIM, 0.8f);
        g_hits.push_back({ cp, HOT_COPY_TURN_BASE + ti });
        RECT mark{ bub.left + 11, bub.top + 11, bub.left + 14, bub.bottom - 11 };
        FillRound(g, mark, C_ACCENT, 1.5f);
        int ly = bub.top + 13;
        for (auto& l : t.userLines) {
            int rx = bub.left + 26;
            for (auto& r : l.runs) {
                TextAt(hdc, rx, ly, r.text, r.font, r.col);
                rx += TextW(hdc, r.text, r.font);
            }
            ly += LH_CHAT;
        }
        y += bh + 24;
    }

    // assistant identity row
    IconAnvil(g, (float)x0 + 9, (float)y + 11, 0.62f, C_ACCENT);
    TextAt(hdc, x0 + 26, y + 4, L"FLETCH", F_LABEL, C_MUTED);
    if (!t.running && t.ms > 0) {
        std::wstring meta = DurStr(t.ms);
        int labelW = TextW(hdc, L"FLETCH", F_LABEL);
        TextAt(hdc, x0 + 26 + labelW + 14, y + 4, meta, F_SMALL, C_DIM);
    }
    y += 26;

    if (ShowSteps(t) && !t.steps.empty()) {
        int railH = 0;
        for (auto& s : t.steps) railH += StepHeight(hdc, s) + 2;
        railH -= 18;
        if (railH > 10) LineC(g, x0 + 13, y + 10, x0 + 13, y + 10 + railH, RGB(46, 41, 34));

        int si = 0;
        for (auto& s : t.steps) {
            int sh = StepHeight(hdc, s);
            PaintStep(hdc, g, s, x0, y, w, si, ti);
            if (s.kind == Step::Tool)
                g_hits.push_back({ { x0, y, x0 + w, y + 30 }, HOT_STEP_BASE + ti * 100 + si });
            y += sh + 2; ++si;
        }
        y += 6;
    }

    if (!t.running && !t.steps.empty() && !t.expanded) {
        std::wstring head = std::to_wstring(t.steps.size()) + (t.steps.size() == 1 ? L" step" : L" steps");
        head += L"  \x00B7  " + std::to_wstring(t.tools) + L" tool" + (t.tools == 1 ? L"" : L"s");
        head += L"  \x00B7  " + DurStr(t.ms);
        if (t.fails) head += L"  \x00B7  " + std::to_wstring(t.fails) + L" failed";
        int pw = TextW(hdc, head, F_SMALLB) + 44;
        RECT pill{ x0, y, x0 + pw, y + 28 };
        bool hov = g_hover == HOT_TURN_BASE + ti;
        FillRound(g, pill, hov ? C_CARD_H : C_CARD, 14);
        StrokeRound(g, pill, hov ? C_LINE2 : C_LINE, 1, 14);
        IconChevron(g, (float)pill.left + 16, (float)pill.top + 14, false,
                    t.fails ? C_ERR : C_MUTED, 0.9f);
        TextAt(hdc, pill.left + 28, pill.top + 6, head, F_SMALLB, t.fails ? C_ERR : C_MUTED);
        g_hits.push_back({ { pill.left, pill.top, pill.right + 40, pill.bottom }, HOT_TURN_BASE + ti });
        y += 36;
    }

    if (!t.answer.empty()) {
        int n = (int)t.answerLines.size();
        std::vector<int> ys(n);
        int yy = y;
        for (int i = 0; i < n; ++i) {
            ys[i] = yy;
            yy += t.answerLines[i].code ? LH_MONO : LH_CHAT;
        }
        for (int i = 0; i < n; ++i) {
            if (!t.answerLines[i].code) continue;
            int j = i;
            int maxw = 0;
            while (j < n && t.answerLines[j].code) { maxw = (std::max)(maxw, t.answerLines[j].w); ++j; }
            int bw = (std::min)(w, (std::max)(maxw + 28, 280));
            RECT box{ x0, ys[i] - 5, x0 + bw, ys[j - 1] + LH_MONO + 4 };
            FillRound(g, box, RGB(18, 16, 13), 8);
            StrokeRound(g, box, C_LINE, 1, 8);
            i = j - 1;
        }
        for (int i = 0; i < n; ++i) {
            Line& l = t.answerLines[i];
            int rx = x0 + l.indent + (l.code ? 14 : 0);
            for (auto& r : l.runs) {
                if (!r.text.empty()) {
                    TextAt(hdc, rx, ys[i], r.text, r.font, r.col);
                    rx += TextW(hdc, r.text, r.font);
                }
            }
        }
        y = yy;
        if (t.running && (NowMs() % 900) < 520) {
            int lastW = t.answerLines.empty() ? 0 : t.answerLines.back().w;
            RECT caret{ x0 + lastW + 3, y - LH_CHAT + 5, x0 + lastW + 9, y - 3 };
            FillRound(g, caret, C_ACCENT, 2);
        }
        y += 14;
    }

    if (t.running) {
        float cy = (float)y + 12;
        IconSpinner(g, (float)x0 + 10, cy, C_ACCENT, 0.9f);
        std::wstring lbl = t.liveThought;
        if (lbl.empty()) lbl = t.answer.empty() ? g_status : L"writing";
        if (lbl.rfind(L"Step ", 0) == 0) lbl = L"thinking";
        TextAt(hdc, x0 + 26, y + 3, Ellipsize(hdc, lbl, w - 40, F_SMALL), F_SMALL, C_MUTED);
        y += 28;
    }
    return y - startY + 22;
}

// empty-state suggestion cards
struct Idea { const wchar_t* title; const wchar_t* sub; int icon; const wchar_t* prompt; };
static const Idea IDEAS[3] = {
    { L"Download media", L"Audio or video, straight into your folders.",
      0, L"Download the audio from a YouTube video about the history of the anvil and save it as mp3 in my workspace." },
    { L"Research and save", L"Reads the web, writes a clean brief.",
      1, L"Research the history of the anvil and save a short brief as a Word document in my workspace." },
    { L"Tidy a folder", L"Group loose files into folders by type.",
      2, L"Tidy my Downloads folder: sort files into folders by type and tell me what moved." },
};

static void PaintEmpty(HDC hdc, Graphics& g, RECT view, int x0, int w) {
    int vh = view.bottom - view.top;
    int top = view.top + (std::max)(34, (vh - 392) / 2 - 6);

    for (int i = 3; i >= 1; --i) {
        RECT halo{ x0 + 16 - i * 9, top + 6 - i * 9, x0 + 66 + i * 9, top + 56 + i * 9 };
        FillRoundA(g, halo, C_EMBER, (BYTE)(9 + (3 - i) * 6), (float)(26 + i * 9));
    }
    RECT badge{ x0 + 16, top + 6, x0 + 66, top + 56 };
    FillRoundGrad(g, badge, RGB(58, 41, 20), RGB(40, 28, 15), 14, false);
    StrokeRound(g, badge, RGB(94, 66, 32), 1, 14);
    IconAnvil(g, (float)x0 + 41, (float)top + 31, 1.55f, C_ACCENT);

    TextAt(hdc, x0, top + 74, L"Put it to work.", F_H1, C_TEXT);
    TextAt(hdc, x0, top + 116,
           L"Describe the outcome. It plans, uses your machine, and reports back.",
           F_UI, C_MUTED);

    int cardW = (w - 24) / 3;
    int cy = top + 166;
    for (int i = 0; i < 3; ++i) {
        RECT card{ x0 + i * (cardW + 12), cy, x0 + i * (cardW + 12) + cardW, cy + 112 };
        bool hov = g_hover == HOT_SUGGEST_BASE + i;
        FillRound(g, card, hov ? C_CARD_H : C_CARD, 12);
        StrokeRound(g, card, hov ? RGB(120, 84, 38) : C_LINE, 1, 12);
        COLORREF ic = hov ? C_ACCENT : C_MUTED;
        if (IDEAS[i].icon == 0) IconDownload(g, (float)card.left + 20, (float)card.top + 22, ic, 1.1f);
        else if (IDEAS[i].icon == 1) IconGlobe(g, (float)card.left + 20, (float)card.top + 22, ic, 1.1f);
        else IconFolder(g, (float)card.left + 20, (float)card.top + 22, ic, 1.1f);
        TextAt(hdc, card.left + 16, card.top + 44, IDEAS[i].title, F_UIB, C_TEXT);
        RECT sr{ card.left + 16, card.top + 66, card.right - 14, card.bottom - 8 };
        HFONT old = (HFONT)SelectObject(hdc, F_SMALL);
        SetTextColor(hdc, C_MUTED); SetBkMode(hdc, TRANSPARENT);
        DrawTextW(hdc, IDEAS[i].sub, -1, &sr, DT_LEFT | DT_TOP | DT_WORDBREAK);
        SelectObject(hdc, old);
        IconChevron(g, (float)card.right - 16, (float)card.top + 22, false, hov ? C_ACCENT : C_DIM, 0.9f);
        g_hits.push_back({ card, HOT_SUGGEST_BASE + i });
    }

    TextAt(hdc, x0, cy + 138,
           L"Runs on this machine. Nothing is sent anywhere unless a task needs the web.",
           F_SMALL, C_DIM);
}

static void PaintTranscript(HDC hdc, Graphics& g, RECT view) {
    int x0 = ColX();
    int w = ColW();
    if (w < 260) w = 260;

    if (g_turns.empty()) { PaintEmpty(hdc, g, view, x0, w); return; }

    int total = 0;
    std::vector<int> heights(g_turns.size());
    for (size_t i = 0; i < g_turns.size(); ++i) {
        heights[i] = MeasureTurn(hdc, g_turns[i], w);
        total += heights[i];
    }
    g_contentH = total;
    int vh = view.bottom - view.top;
    int maxScroll = (std::max)(0, total - vh + 12);
    if (g_follow) g_scroll = maxScroll;
    if (g_scroll > maxScroll) g_scroll = maxScroll;
    if (g_scroll < 0) g_scroll = 0;

    int y = view.top + 12 - g_scroll;
    for (size_t i = 0; i < g_turns.size(); ++i) {
        int h = heights[i];
        if (y + h > view.top && y < view.bottom)
            PaintTurn(hdc, g, g_turns[i], x0, y, w, (int)i);
        y += h;
    }
}

// ---------------------------------------------------------------------------
// sidebar
// ---------------------------------------------------------------------------
static void PaintSidebar(HDC hdc, Graphics& g, RECT side) {
    FillRectC(g, side, C_SIDE);
    LineC(g, side.right - 1, 0, side.right - 1, side.bottom, RGB(30, 27, 22));

    RECT badge{ 22, 20, 60, 58 };
    FillRoundGrad(g, badge, C_ACCENT_H, C_EMBER, 11);
    IconAnvil(g, 41, 39, 0.95f, C_INK);
    TextAt(hdc, 72, 24, L"Anvil Fletch", F_H2, C_TEXT);
    TextAt(hdc, 73, 45, L"LOCAL AGENT", F_LABEL, RGB(196, 132, 62));
    LineC(g, 22, 76, side.right - 22, 76, RGB(33, 29, 24));

    int y = 92;
    RECT nb{ 22, y, side.right - 22, y + 42 };
    bool hNew = g_hover == HOT_NEW;
    FillRoundGrad(g, nb, hNew ? RGB(255, 198, 120) : C_ACCENT_H,
                  hNew ? RGB(242, 128, 60) : C_EMBER, 11);
    IconPlus(g, (float)nb.left + 22, (float)nb.top + 21, C_INK, 0.95f);
    TextAt(hdc, nb.left + 38, nb.top + 12, L"New task", F_UIB, C_INK);
    TextAt(hdc, nb.right - 54, nb.top + 14, L"Ctrl N", F_SMALL, RGB(126, 74, 32));
    g_hitsSide.push_back({ nb, HOT_NEW });
    y += 60;

    auto Section = [&](const wchar_t* label) {
        TextAt(hdc, 24, y, label, F_LABEL, C_DIM);
        y += 22;
    };
    auto Row = [&](const std::wstring& title, const std::wstring& sub, int icon,
                   COLORREF tint, int id, float ratio) {
        RECT r{ 22, y, side.right - 22, y + 54 };
        bool hov = g_hover == id;
        FillRound(g, r, hov ? C_CARD : C_PANEL, 10);
        StrokeRound(g, r, hov ? C_LINE2 : C_LINE, 1, 10);
        float cx = (float)r.left + 20, cy = (float)r.top + 27;
        if (icon == 0) IconFolder(g, cx, cy, tint, 1.0f);
        else IconChip(g, cx, cy, tint, 1.0f);
        TextAt(hdc, r.left + 40, r.top + 9,
               Ellipsize(hdc, title, (int)((r.right - r.left - 60) * ratio), F_UIB), F_UIB, C_TEXT);
        TextAt(hdc, r.left + 40, r.top + 29,
               Ellipsize(hdc, sub, r.right - r.left - 56, F_SMALL), F_SMALL, C_MUTED);
        IconChevron(g, (float)r.right - 18, cy, id == HOT_MODEL ? g_modelMenu : false, C_DIM, 0.9f);
        g_hitsSide.push_back({ r, id });
        y += 62;
    };

    Section(L"WORKSPACE");
    std::wstring wsDisp = g_wsPath.empty() ? L"Not set" : BaseName(g_wsPath);
    std::wstring wsSub = g_wsPath.empty() ? L"choose a folder" : g_wsPath;
    if (wsSub.size() > 40) wsSub = L"..." + wsSub.substr(wsSub.size() - 36);
    Row(wsDisp, wsSub, 0, C_ACCENT, HOT_WS, 0.72f);

    Section(L"MODEL");
    Row(g_model, g_online ? L"server online" : L"server offline", 1,
        g_online ? C_OK : C_WARN, HOT_MODEL, 0.78f);

    Section(L"ENGINE");
    {
        RECT r{ 22, y, side.right - 22, y + 56 };
        FillRound(g, r, C_PANEL, 10);
        StrokeRound(g, r, C_LINE, 1, 10);
        wchar_t a[32], b[32];
        swprintf(a, 32, L"%d", g_toolCount);
        swprintf(b, 32, L"%d", g_lessonCount);
        int mid = (r.left + r.right) / 2;
        TextAt(hdc, r.left + 18, r.top + 10, a, F_H2, C_TEXT);
        TextAt(hdc, r.left + 18, r.top + 34, L"TOOLS", F_LABEL, C_DIM);
        LineC(g, mid, r.top + 12, mid, r.bottom - 12, C_LINE);
        TextAt(hdc, mid + 18, r.top + 10, b, F_H2, C_TEXT);
        TextAt(hdc, mid + 18, r.top + 34, L"LESSONS", F_LABEL, C_DIM);
        y += 66;
    }

    Section(L"STUDY LOOP");
    {
        RECT r{ 22, y, side.right - 22, y + 54 };
        bool hov = g_hover == HOT_STUDY;
        FillRound(g, r, hov ? C_CARD : C_PANEL, 10);
        StrokeRound(g, r, g_study ? RGB(112, 78, 36) : (hov ? C_LINE2 : C_LINE), 1, 10);
        IconLayers(g, (float)r.left + 20, (float)r.top + 27,
                   g_study ? C_ACCENT : C_MUTED, 1.0f);
        TextAt(hdc, r.left + 40, r.top + 9, L"Study loop", F_UIB, C_TEXT);
        TextAt(hdc, r.left + 40, r.top + 29,
               Ellipsize(hdc, g_study ? L"learning C++ on its own" : L"off",
                         r.right - r.left - 100, F_SMALL),
               F_SMALL, g_study ? RGB(196, 132, 62) : C_MUTED);
        RECT sw{ r.right - 52, r.top + 18, r.right - 16, r.top + 36 };
        FillRound(g, sw, g_study ? C_ACCENT : RGB(48, 43, 36), 9);
        StrokeRound(g, sw, g_study ? C_ACCENT_H : C_LINE2, 1, 9);
        float kx = g_study ? (float)sw.right - 9 : (float)sw.left + 9;
        EllipseC(g, kx, (float)(sw.top + sw.bottom) / 2, 6.5f, g_study ? C_INK : RGB(118, 106, 90));
        g_hitsSide.push_back({ r, HOT_STUDY });
        y += 62;
    }

    int fy = side.bottom - 42;
    TextAt(hdc, 24, y, L"RECENT", F_LABEL, C_DIM);
    {
        bool hov = g_hover == HOT_CLEAR_RECENTS;
        bool any = !g_recents.empty();
        RECT clr{ side.right - 72, y - 6, side.right - 22, y + 16 };
        if (hov && any) FillRound(g, clr, C_CARD, 7);
        TextAt(hdc, clr.left + 10, y - 1, L"Clear", F_LABEL,
               !any ? RGB(66, 61, 52) : (hov ? C_ACCENT : C_MUTED));
        g_hitsSide.push_back({ clr, HOT_CLEAR_RECENTS });
    }
    y += 22;
    if (g_recents.empty()) TextAt(hdc, 26, y + 2, L"Nothing yet", F_SMALL, C_DIM);
    int room = (std::max)(0, fy - 14 - y);
    int fit = (std::min)((int)g_recents.size(), room / 34);
    for (int i = 0; i < fit; ++i) {
        RECT r{ 22, y, side.right - 22, y + 32 };
        bool hov = g_hover == HOT_RECENT_BASE + i;
        bool cpHot = g_hover == HOT_COPY_RECENT_BASE + i;
        if (hov || cpHot) FillRound(g, r, C_CARD, 8);
        IconClock(g, (float)r.left + 15, (float)r.top + 16, hov ? C_ACCENT : C_DIM, 0.8f);
        TextAt(hdc, r.left + 32, r.top + 8,
               Ellipsize(hdc, g_recents[i], r.right - r.left - 48, F_SMALL),
               F_SMALL, hov ? C_TEXT : C_MUTED);
        RECT cp{ r.right - 28, r.top + 6, r.right - 8, r.top + 26 };
        if (cpHot) FillRound(g, cp, C_CARD_H, 6);
        IconCopy(g, (float)(cp.left + cp.right) / 2, (float)(cp.top + cp.bottom) / 2,
                 cpHot ? C_TEXT : RGB(74, 66, 55), 0.78f);
        g_hitsSide.push_back({ r, HOT_RECENT_BASE + i });
        g_hitsSide.push_back({ cp, HOT_COPY_RECENT_BASE + i });
        y += 34;
    }

    LineC(g, 22, fy - 10, side.right - 22, fy - 10, RGB(30, 27, 22));
    EllipseC(g, 26, fy + 11, 2.4f, g_online ? C_OK : C_DIM);
    TextAt(hdc, 36, fy + 4, g_online ? L"LOCAL \x00B7 PRIVATE" : L"WAITING FOR ENGINE",
           F_LABEL, C_DIM);
}

// ---------------------------------------------------------------------------
// header
// ---------------------------------------------------------------------------
static void PaintHeader(HDC hdc, Graphics& g, RECT head) {
    FillRectC(g, head, C_APP);
    LineC(g, head.left, head.bottom - 1, head.right, head.bottom - 1, RGB(30, 27, 22));

    int x0 = ColX();
    std::wstring title = L"New task";
    if (!g_turns.empty() && !g_turns.back().user.empty())
        title = Trim(g_turns.back().user);
    TextAt(hdc, x0, 12, Ellipsize(hdc, title, ColW() - 260, F_UIB), F_UIB, C_TEXT);

    std::wstring wsName = g_wsPath.empty() ? L"no folder" : BaseName(g_wsPath);
    std::wstring sub = g_dead ? L"engine offline"
                     : (g_busy ? g_status : g_model + L"  \x00B7  " + wsName);
    if (sub.empty()) sub = L"ready";
    TextAt(hdc, x0, 35, Ellipsize(hdc, sub, ColW() - 260, F_SMALL), F_SMALL, C_DIM);

    std::wstring pill = g_busy ? L"on it" : (g_dead ? L"offline" : L"standing by");
    COLORREF pc = g_busy ? C_ACCENT : (g_dead ? C_ERR : C_OK);
    int pw = TextW(hdc, pill, F_SMALLB) + 44;
    RECT pr{ head.right - PAD - pw, 18, head.right - PAD, 44 };
    FillRound(g, pr, C_PANEL, 13);
    StrokeRound(g, pr, C_LINE, 1, 13);

    int dx = pr.left + 16, dy = pr.top + 13;
    if (g_busy) {
        long long ph = NowMs() % 1200;
        float k = (float)((ph < 600) ? ph : 1200 - ph) / 600.0f;
        EllipseC(g, dx, dy, 4.0f + k * 3.2f, C_ACCENT);
        EllipseC(g, dx, dy, 3.2f, C_ACCENT_H);
    } else {
        EllipseC(g, dx, dy, 3.4f, pc);
    }
    TextAt(hdc, pr.left + 28, pr.top + 6, pill, F_SMALLB, pc);

    if (g_busy && !g_turns.empty() && g_turns.back().running) {
        std::wstring el = ClockStr(NowMs() - g_turns.back().started);
        int ew = TextW(hdc, el, F_MONOS);
        TextAt(hdc, pr.left - 16 - ew, pr.top + 7, el, F_MONOS, C_DIM);
    }
}

// ---------------------------------------------------------------------------
// composer
// ---------------------------------------------------------------------------
static void PaintComposer(HDC hdc, Graphics& g, RECT comp) {
    bool focused = (GetFocus() == g_edit);
    if (focused) {
        for (int i = 3; i >= 1; --i) {
            RECT gl{ comp.left - i, comp.top - i, comp.right + i, comp.bottom + i };
            StrokeRoundA(g, gl, C_ACCENT, (BYTE)(14 + (3 - i) * 6), 1.0f, 16 + i);
        }
    }
    FillRound(g, comp, C_PANEL, 16);
    StrokeRound(g, comp, focused ? RGB(96, 71, 38) : C_LINE, focused ? 1.3f : 1, 16);

    RECT ab{ comp.right - 108, comp.bottom - 52, comp.right - 66, comp.bottom - 12 };
    {
        bool ah = g_hover == HOT_ATTACH;
        bool hasAtt = !g_attachments.empty();
        FillRound(g, ab, ah ? C_CARD_H : C_CARD, 19);
        StrokeRound(g, ab, hasAtt ? RGB(112, 78, 36) : (ah ? C_LINE2 : C_LINE), 1, 19);
        TextAt(hdc, ab.left + 13, ab.top + 10, L"\xE723", F_GLYPH,
               hasAtt ? C_ACCENT : (ah ? C_TEXT : C_MUTED));
        if (hasAtt) {
            std::wstring num = std::to_wstring(g_attachments.size());
            int tw = TextW(hdc, num, F_SMALLB);
            int bw = (std::max)(tw + 10, 18);
            RECT bg{ ab.right - bw + 6, ab.top - 6, ab.right + 6, ab.top + 12 };
            FillRound(g, bg, C_ACCENT, 9);
            TextAt(hdc, (bg.left + bg.right - tw) / 2, bg.top + 2, num, F_SMALLB, C_INK);
        }
        g_hits.push_back({ ab, HOT_ATTACH });
    }

    RECT btn{ comp.right - 56, comp.bottom - 52, comp.right - 16, comp.bottom - 12 };
    if (g_busy) {
        bool h = g_hover == HOT_STOP;
        FillRound(g, btn, h ? RGB(58, 30, 25) : RGB(44, 25, 21), 19);
        StrokeRound(g, btn, h ? RGB(150, 66, 54) : RGB(96, 48, 38), 1, 19);
        IconStop(g, (float)(btn.left + btn.right) / 2, (float)(btn.top + btn.bottom) / 2, RGB(255, 138, 118));
        g_hits.push_back({ btn, HOT_STOP });
    } else {
        bool h = g_hover == HOT_SEND;
        FillRoundGrad(g, btn, h ? RGB(255, 198, 120) : C_ACCENT_H,
                      h ? RGB(240, 126, 60) : C_EMBER, 19);
        IconSend(g, (float)(btn.left + btn.right) / 2, (float)(btn.top + btn.bottom) / 2, C_INK);
        g_hits.push_back({ btn, HOT_SEND });
    }

    std::wstring hint = g_busy ? L"Esc or the button stops the run"
                               : L"Enter to run    \x00B7    Shift + Enter for a new line";
    if (!g_busy && g_study) hint = L"Study loop on    \x00B7    " + hint;
    int hintW = ab.left - (comp.left + 20) - 14;
    TextAt(hdc, comp.left + 20, comp.bottom - 27,
           Ellipsize(hdc, hint, hintW, F_SMALL), F_SMALL,
           g_study && !g_busy ? RGB(196, 132, 62) : C_DIM);
}

static void PaintModelMenu(HDC hdc, Graphics& g, RECT anchor) {
    int itemH = 36;
    int n = (std::max)(1, (int)g_models.size());
    RECT m{ anchor.left, anchor.bottom + 6, anchor.right, anchor.bottom + 12 + n * itemH };
    FillRound(g, m, RGB(26, 23, 19), 10);
    StrokeRound(g, m, C_LINE2, 1, 10);
    if (g_models.empty()) {
        TextAt(hdc, m.left + 14, m.top + 10, L"No models - is Ollama running?", F_SMALL, C_MUTED);
        return;
    }
    for (int i = 0; i < (int)g_models.size(); ++i) {
        RECT r{ m.left + 5, m.top + 5 + i * itemH, m.right - 5, m.top + 5 + (i + 1) * itemH - 2 };
        bool sel = IEquals(g_models[i], g_model);
        if (g_hover == HOT_PICK_BASE + i) FillRound(g, r, C_CARD_H, 7);
        TextAt(hdc, r.left + 12, r.top + 9, g_models[i], sel ? F_UIB : F_UI,
               sel ? C_ACCENT : C_TEXT);
        if (sel) IconCheck(g, (float)r.right - 16, (float)r.top + 18, C_ACCENT, 0.9f);
        g_hits.push_back({ r, HOT_PICK_BASE + i });
    }
}

static std::wstring BytesSpan(long long done, long long total) {
    double unit = 1.0;
    const wchar_t* u = L"B";
    if (total >= 1024LL) { unit = 1024.0; u = L"KB"; }
    if (total >= 1024LL * 1024) { unit = 1024.0 * 1024; u = L"MB"; }
    if (total >= 1024LL * 1024 * 1024) { unit = 1024.0 * 1024 * 1024; u = L"GB"; }
    double a = (double)done / unit, b = (double)total / unit;
    wchar_t buf[64];
    swprintf(buf, 64, b < 10.0 ? L"%.1f/%.1f %s" : L"%.0f/%.0f %s", a, b, u);
    return buf;
}

static int PaintProgress(HDC hdc, Graphics& g, RECT rc, int bottom) {
    if (!g_prog.active) return bottom;
    RECT comp = CompRect();
    RECT box{ comp.left, bottom - 46, comp.right, bottom };
    FillRound(g, box, RGB(31, 28, 23), 12);
    StrokeRound(g, box, RGB(78, 62, 40), 1, 12);

    int pct = 0;
    if (g_prog.total > 0) {
        pct = (int)(g_prog.done * 100 / g_prog.total);
        if (pct < 0) pct = 0;
        if (pct > 100) pct = 100;
    }

    std::wstring label = g_prog.label.empty() ? L"Working" : g_prog.label;
    std::wstring right;
    COLORREF rcol = C_ACCENT;
    if (g_prog.total > 0)
        right = std::to_wstring(pct) + L"%  (" + BytesSpan(g_prog.done, g_prog.total) + L")";
    else if (g_prog.filesTotal > 1)
        right = std::to_wstring(g_prog.filesDone) + L"/" +
                std::to_wstring(g_prog.filesTotal) + L" files";
    else if (!g_prog.item.empty()) {
        right = g_prog.item;
        rcol = C_MUTED;
    }

    int rw = right.empty() ? 0 : TextW(hdc, right, F_SMALLB);
    if (rw > 260) rw = 260;
    int lw = box.right - box.left - 32 - (rw ? rw + 18 : 0);
    TextAt(hdc, box.left + 16, box.top + 8,
           Ellipsize(hdc, label, lw, F_SMALLB), F_SMALLB, C_TEXT);
    if (rw) TextAt(hdc, box.right - 16 - rw, box.top + 8,
                   Ellipsize(hdc, right, rw, F_SMALLB), F_SMALLB, rcol);

    RECT track{ box.left + 16, box.bottom - 15, box.right - 16, box.bottom - 9 };
    FillRound(g, track, RGB(53, 46, 37), 3);
    if (g_prog.total > 0) {
        int w = (track.right - track.left) * pct / 100;
        if (w > 2) {
            RECT fill{ track.left, track.top, track.left + w, track.bottom };
            FillRoundGrad(g, fill, C_ACCENT_H, C_EMBER, 3);
        }
    } else {
        int span = (std::max)(1, (int)(track.right - track.left));
        int w = (std::min)((std::max)(26, span / 4), span);
        long long t = NowMs() % 1500;
        float k = (float)t / 1500.0f;
        if (k > 0.5f) k = 1.0f - k;
        int x = track.left + (int)((span - w) * (k * 2.0f));
        RECT fill{ x, track.top, x + w, track.bottom };
        FillRound(g, fill, RGB(198, 134, 62), 3);
    }
    return box.top - 8;
}

static std::map<std::wstring, Image*> g_thumbs;
static bool IsImagePath(const std::wstring& p);

static Image* ThumbFor(const std::wstring& path) {
    auto it = g_thumbs.find(path);
    if (it != g_thumbs.end()) return it->second;
    Image* img = nullptr;
    if (IsImagePath(path)) {
        img = new Image(path.c_str());
        if (img->GetLastStatus() != Ok) { delete img; img = nullptr; }
    }
    g_thumbs[path] = img;
    return img;
}

static int PaintAttachments(HDC hdc, Graphics& g, RECT rc, int bottom) {
    if (g_attachments.empty()) return bottom;
    RECT comp = CompRect();
    const int chipH = 54, thumb = 42, gap = 8, rowGap = 6, maxRows = 3;
    struct Chip { int w; };
    std::vector<int> widths;
    for (auto& path : g_attachments) {
        std::wstring name = BaseName(path);
        int nw = (std::min)(TextW(hdc, name, F_SMALL), 126);
        widths.push_back(12 + (IsImagePath(path) ? thumb + 8 : 14) + nw + 30);
    }
    int span = comp.right - comp.left;
    std::vector<std::vector<int>> rows;
    int x = 0;
    for (int i = 0; i < (int)widths.size(); ++i) {
        if (rows.empty() || x + widths[i] > span) {
            rows.push_back({});
            x = 0;
        }
        rows.back().push_back(i);
        x += widths[i] + gap;
    }
    int hidden = 0;
    if ((int)rows.size() > maxRows) {
        for (size_t r = maxRows; r < rows.size(); ++r) hidden += (int)rows[r].size();
        rows.resize(maxRows);
    }
    int totalH = (int)rows.size() * chipH + ((int)rows.size() - 1) * rowGap;
    int top = bottom - totalH;
    InterpolationMode oldInterp = g.GetInterpolationMode();
    g.SetInterpolationMode(InterpolationModeHighQualityBicubic);
    int y = top;
    for (size_t r = 0; r < rows.size(); ++r) {
        int cx = comp.left;
        for (size_t k = 0; k < rows[r].size(); ++k) {
            int i = rows[r][k];
            int pw = widths[i];
            bool last = (r + 1 == rows.size() && k + 1 == rows[r].size() && hidden);
            if (last) pw = (std::max)(pw, 150);
            RECT p{ cx, y, cx + pw, y + chipH };
            bool hov = g_hover == HOT_ATTACH_BASE + i;
            FillRound(g, p, hov ? RGB(58, 40, 30) : C_CARD, 12);
            StrokeRound(g, p, hov ? RGB(152, 82, 48) : RGB(76, 62, 42), 1, 12);
            int tx = p.left + 9;
            Image* img = ThumbFor(g_attachments[i]);
            RECT th{ tx, p.top + 6, tx + thumb, p.top + 6 + thumb };
            if (img) {
                FillRound(g, th, RGB(12, 11, 9), 7);
                g.DrawImage(img, Rect((int)th.left + 1, (int)th.top + 1, thumb - 2, thumb - 2),
                            0, 0, (int)img->GetWidth(), (int)img->GetHeight(), UnitPixel);
                StrokeRound(g, th, RGB(84, 68, 46), 1, 7);
            } else {
                FillRound(g, th, RGB(24, 22, 18), 7);
                TextAt(hdc, th.left + 13, th.top + 11, L"\xE8A5", F_GLYPH, C_MUTED);
            }
            tx += thumb + 9;
            std::wstring name = BaseName(g_attachments[i]);
            if (last) {
                TextAt(hdc, tx, p.top + 18, std::to_wstring(hidden) + L" more",
                       F_SMALL, C_MUTED);
            } else {
                TextAt(hdc, tx, p.top + 18,
                       Ellipsize(hdc, name, pw - (tx - p.left) - 26, F_SMALL),
                       F_SMALL, C_TEXT);
                TextAt(hdc, p.right - 19, p.top + 16, L"\xE711", F_GLYPH,
                       hov ? C_ERR : C_DIM);
                g_hits.push_back({ p, HOT_ATTACH_BASE + i });
            }
            cx += pw + gap;
        }
        y += chipH + rowGap;
    }
    g.SetInterpolationMode(oldInterp);
    return top - 8;
}

static void PaintToast(HDC hdc, Graphics& g, RECT rc, int bottom) {
    if (g_toast.empty() || GetTickCount64() >= g_toastUntil) return;
    int tw = TextW(hdc, g_toast, F_SMALLB);
    int bw = (std::min)(tw + 64, (int)rc.right - 80);
    RECT box{ rc.right / 2 - bw / 2, bottom - 34, rc.right / 2 + bw / 2, bottom };
    FillRound(g, box, RGB(34, 30, 25), 16);
    StrokeRound(g, box, C_LINE2, 1, 16);
    IconCheck(g, (float)box.left + 20, (float)(box.top + box.bottom) / 2, C_OK, 0.72f);
    g.Flush();
    TextAt(hdc, box.left + 33, box.top + 7, Ellipsize(hdc, g_toast, bw - 46, F_SMALLB),
           F_SMALLB, C_TEXT);
}

// ---------------------------------------------------------------------------
// main paint
// ---------------------------------------------------------------------------

static void OnPaint(HWND hwnd) {
    ULONGLONG t0 = GetTickCount64();
    PAINTSTRUCT ps;
    HDC hdc = BeginPaint(hwnd, &ps);
    RECT rc; GetClientRect(hwnd, &rc);
    if (rc.right <= 0 || rc.bottom <= 0) { EndPaint(hwnd, &ps); return; }

    g_spinners.clear();
    g_recordSpinners = true;

    HDC mem = CreateCompatibleDC(hdc);
    BITMAPINFO bi{};
    bi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
    bi.bmiHeader.biWidth = rc.right;
    bi.bmiHeader.biHeight = -rc.bottom;
    bi.bmiHeader.biPlanes = 1;
    bi.bmiHeader.biBitCount = 32;
    bi.bmiHeader.biCompression = BI_RGB;
    void* bits = NULL;
    HBITMAP bmp = CreateDIBSection(hdc, &bi, DIB_RGB_COLORS, &bits, NULL, 0);
    if (!bmp) { EndPaint(hwnd, &ps); return; }
    HGDIOBJ oldBmp = SelectObject(mem, bmp);

    ULONGLONG p0 = 0, p1 = 0, p2 = 0, p3 = 0, p4 = 0, p5 = 0,
              p6 = 0, p7 = 0, p8 = 0, p9 = 0, p10 = 0;
    RECT view = ViewRect();
    int ovY = 0;
    {
        Graphics g(mem);
        g.SetSmoothingMode(SmoothingModeAntiAlias);
        g.SetTextRenderingHint(TextRenderingHintClearTypeGridFit);
        g.SetPixelOffsetMode(PixelOffsetModeHalf);

        g_textOps.clear();
        g_textCollect = true;

        FillRectC(g, rc, C_APP);
        g_hits.clear(); g_hitsSide.clear();

        {
            SolidBrush sheet(Color(255, GetRValue(C_SHEET), GetGValue(C_SHEET), GetBValue(C_SHEET)));
            g.FillRectangle(&sheet, (REAL)view.left, (REAL)0,
                            (REAL)(rc.right - view.left), (REAL)rc.bottom);
        }

        p0 = GetTickCount64();
        PaintSidebar(mem, g, SideRect());
        p1 = GetTickCount64();
        PaintHeader(mem, g, HeadRect());
        p2 = GetTickCount64();

        {
            Region save;
            g.GetClip(&save);
            Region clip(RectF((REAL)view.left, (REAL)view.top,
                              (REAL)(view.right - view.left), (REAL)(view.bottom - view.top)));
            g.SetClip(&clip);
            // GDI text (TextAt) ignores the GDI+ clip, so clip the HDC as well.
            int dcSave = SaveDC(mem);
            IntersectClipRect(mem, view.left, view.top, view.right, view.bottom);
            PaintTranscript(mem, g, view);

            const int fh = 22;
            for (int i = 0; i < fh; ++i) {
                float k = (float)i / (float)(fh - 1);
                int r = (int)(GetRValue(C_SHEET) + (GetRValue(C_APP) - GetRValue(C_SHEET)) * k);
                int gr = (int)(GetGValue(C_SHEET) + (GetGValue(C_APP) - GetGValue(C_SHEET)) * k);
                int b = (int)(GetBValue(C_SHEET) + (GetBValue(C_APP) - GetBValue(C_SHEET)) * k);
                SolidBrush row(Color(255, r, gr, b));
                g.FillRectangle(&row, (REAL)view.left, (REAL)(view.bottom - fh + i),
                                (REAL)(view.right - view.left), 1.0f);
            }
            if (g_scroll > 0) {
                const int th = 16;
                for (int i = 0; i < th; ++i) {
                    float k = (float)i / (float)(th - 1);
                    int r = (int)(GetRValue(C_APP) + (GetRValue(C_SHEET) - GetRValue(C_APP)) * k);
                    int gr = (int)(GetGValue(C_APP) + (GetGValue(C_SHEET) - GetGValue(C_APP)) * k);
                    int b = (int)(GetBValue(C_APP) + (GetBValue(C_SHEET) - GetBValue(C_APP)) * k);
                    SolidBrush row(Color(255, r, gr, b));
                    g.FillRectangle(&row, (REAL)view.left, (REAL)(view.top + i),
                                    (REAL)(view.right - view.left), 1.0f);
                }
            }
            g.Flush();
            RestoreDC(mem, dcSave);
            g.SetClip(&save);
        }
        p3 = GetTickCount64();

        PaintComposer(mem, g, CompRect());
        p4 = GetTickCount64();

        ovY = CompRect().top - 10;
        ovY = PaintProgress(mem, g, rc, ovY);
        ovY = PaintAttachments(mem, g, rc, ovY);

        int vh = view.bottom - view.top;
        if (g_contentH > vh + 4) {
            int maxS = (std::max)(1, g_contentH - vh + 12);
            int x = ColX() + ColW() + 10;
            if (x > rc.right - 14) x = rc.right - 14;
            int trackH = vh - 24;
            int th = (std::max)(40, (int)((float)vh / g_contentH * trackH));
            int ty = view.top + 12 + (int)((float)g_scroll / maxS * (trackH - th));
            RECT tb{ x, ty, x + 4, ty + th };
            FillRound(g, tb, g_follow ? C_LINE2 : C_DIM, 2);
        }
        g.Flush();
        p5 = GetTickCount64();
    }  // Graphics released before GDI text touches the surface.

    FlushTextOps(mem, rc);
    p6 = GetTickCount64();

    // Menus and the toast sit above everything, so they need their own layer
    // with their own text pass.
    {
        Graphics g(mem);
        g.SetSmoothingMode(SmoothingModeAntiAlias);
        g.SetTextRenderingHint(TextRenderingHintClearTypeGridFit);
        g.SetPixelOffsetMode(PixelOffsetModeHalf);
        g_textOps.clear();
        g_textCollect = true;

        if (g_modelMenu) {
            RECT a{ 22, 0, SIDE_W - 22, 0 };
            for (auto& h : g_hitsSide) if (h.id == HOT_MODEL) a = h.rc;
            PaintModelMenu(mem, g, a);
        }
        PaintToast(mem, g, rc, ovY);
        g.Flush();
        p7 = GetTickCount64();
    }
    FlushTextOps(mem, rc);
    g_textCollect = false;
    g_recordSpinners = false;

    // Keep a spinner-free copy of this frame. Animation-only repaints blit the
    // small spinner rects from it instead of rebuilding the whole scene.
    if (!g_frameBmp || g_frameW != rc.right || g_frameH != rc.bottom) {
        if (g_frameDC) { DeleteDC(g_frameDC); g_frameDC = NULL; }
        if (g_frameBmp) { DeleteObject(g_frameBmp); g_frameBmp = NULL; }
        g_frameDC = CreateCompatibleDC(hdc);
        g_frameBmp = CreateCompatibleBitmap(hdc, rc.right, rc.bottom);
        SelectObject(g_frameDC, g_frameBmp);
        g_frameW = rc.right; g_frameH = rc.bottom;
    }
    if (g_frameDC && g_frameBmp) {
        BitBlt(g_frameDC, 0, 0, rc.right, rc.bottom, mem, 0, 0, SRCCOPY);
        g_frameValid = true;
    } else {
        g_frameValid = false;
    }
    if (!g_spinners.empty()) {
        Graphics ga(mem);
        ga.SetSmoothingMode(SmoothingModeAntiAlias);
        for (auto& sp : g_spinners) PaintSpinnerAt(ga, sp.cx, sp.cy, sp.c, sp.s);
        ga.Flush();
    }
    p8 = GetTickCount64();
    {
        DWORD* px = (DWORD*)bits;
        int npx = rc.right * rc.bottom;
        for (int i = 0; i < npx; ++i) px[i] |= 0xFF000000u;
    }
    p9 = GetTickCount64();
    BitBlt(hdc, 0, 0, rc.right, rc.bottom, mem, 0, 0, SRCCOPY);
    p10 = GetTickCount64();

    SelectObject(mem, oldBmp);
    DeleteObject(bmp);
    DeleteDC(mem);
    EndPaint(hwnd, &ps);
    int cost = (int)(GetTickCount64() - t0);
    g_paintCostMs = g_paintCostMs ? (g_paintCostMs * 3 + cost) / 4 : cost;
}

// True when only the spinner phase changed: no new event, no scrolling, no
// progress/toast/menu animation. Then the cached frame plus fresh arcs is enough.
static bool CanAnimateOnly() {
    if (!g_frameValid || !g_frameDC || g_spinners.empty()) return false;
    if (g_paintAgain || g_uiRev != g_lastPaintRev) return false;
    if (g_prog.active || g_modelMenu) return false;
    if (g_toastUntil && GetTickCount64() < g_toastUntil + 250) return false;
    RECT rc = ClientRect();
    if (rc.right != g_frameW || rc.bottom != g_frameH) return false;
    return LiveSpinner();
}

// Blit only the spinner rects from the cached clean frame, then draw fresh arcs.
// This keeps the animation at ~60fps even with a long transcript.
static void DrawAnimationFrame(HWND hwnd) {
    RECT rc = ClientRect();
    if (!g_frameValid || !g_frameDC || rc.right != g_frameW || rc.bottom != g_frameH) {
        InvalidateRect(hwnd, NULL, FALSE);
        return;
    }
    HDC dc = GetDC(hwnd);
    if (!dc) return;
    for (auto& sp : g_spinners) {
        int x = (int)sp.cx - 12, y = (int)sp.cy - 12;
        int w = 24, h = 24;
        if (x < 0) { w += x; x = 0; }
        if (y < 0) { h += y; y = 0; }
        if (x + w > rc.right) w = rc.right - x;
        if (y + h > rc.bottom) h = rc.bottom - y;
        if (w > 0 && h > 0) BitBlt(dc, x, y, w, h, g_frameDC, x, y, SRCCOPY);
    }
    Graphics ga(dc);
    ga.SetSmoothingMode(SmoothingModeAntiAlias);
    for (auto& sp : g_spinners) PaintSpinnerAt(ga, sp.cx, sp.cy, sp.c, sp.s);
    ga.Flush();
    ReleaseDC(hwnd, dc);
}

// ---------------------------------------------------------------------------
// actions
// ---------------------------------------------------------------------------
static void RequestRepaint(bool force) {
    if (!g_hwnd) return;
    ULONGLONG now = GetTickCount64();
    if (force || now - g_lastPaintAt >= 40) {
        g_lastPaintAt = now;
        g_paintAgain = false;
        g_lastPaintRev = g_uiRev;
        InvalidateRect(g_hwnd, NULL, FALSE);
    } else {
        g_paintAgain = true;
    }
}
static void Refresh() {
    RequestRepaint(true);
    if (g_edit) InvalidateRect(g_edit, NULL, FALSE);
}
static void ScrollToEnd() { g_follow = true; g_scroll = g_contentH; }

static std::wstring AttachmentsJson() {
    std::wstring s = L"[";
    for (size_t i = 0; i < g_attachments.size(); ++i) {
        if (i) s += L",";
        s += L"\"" + JEsc(g_attachments[i]) + L"\"";
    }
    return s + L"]";
}

static void Dispatch(const std::wstring& prompt, bool studySession) {
    if (!g_stdinWr || g_dead) {
        g_pending = prompt;
        g_pendingStudy = studySession;
        if (g_dead) Boot();
        return;
    }
    SendCmd(L"{\"action\":\"chat\",\"prompt\":\"" + JEsc(prompt) +
            L"\",\"workspace\":\"" + JEsc(g_wsPath) +
            L"\",\"model\":\"" + JEsc(g_model) +
            L"\",\"study\":" + (g_study ? L"true" : L"false") +
            L",\"study_session\":" + (studySession ? L"true" : L"false") +
            L",\"attachments\":" + AttachmentsJson() + L"}");
}

static void StartTurn(const std::wstring& prompt, bool studySession) {
    if (g_busy) return;
    Turn t;
    t.user = prompt;
    if (!g_attachments.empty())
        t.user += L"  (" + std::to_wstring(g_attachments.size()) + L" attached)";
    t.running = true;
    t.studySession = studySession;
    t.started = NowMs();
    g_turns.push_back(t);
    PushRecent(prompt);
    g_busy = true; g_status = L"thinking";
    g_prog.active = false;
    g_follow = true;
    bool sentNow = (g_stdinWr && !g_dead);
    Dispatch(prompt, studySession);
    if (sentNow) g_attachments.clear();
    Refresh();
}

static void NewChat() {
    if (g_busy) SendCmd(L"{\"action\":\"stop\"}");
    g_pending.clear(); g_pendingStudy = false;
    g_studyStartPending = false;
    g_study = false;
    g_attachments.clear();
    g_prog.active = false;
    SaveSettings();
    g_turns.clear();
    g_scroll = 0; g_contentH = 0; g_follow = true;
    g_busy = false; g_status = L"ready";
    SendCmd(L"{\"action\":\"new_chat\"}");
    if (g_edit) SetFocus(g_edit);
    Refresh();
}

static void StopAgent() {
    SendCmd(L"{\"action\":\"stop\"}");
    g_status = L"stopping";
    if (!g_turns.empty() && g_turns.back().studySession && g_study) {
        g_study = false;
        g_studyStartPending = false;
        SaveSettings();
    }
    Refresh();
}

static void PickFolder() {
    CoInitializeEx(NULL, COINIT_APARTMENTTHREADED);
    IFileOpenDialog* dlg = NULL;
    if (SUCCEEDED(CoCreateInstance(CLSID_FileOpenDialog, NULL, CLSCTX_INPROC_SERVER,
                                   IID_PPV_ARGS(&dlg)))) {
        DWORD opt; dlg->GetOptions(&opt);
        dlg->SetOptions(opt | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM);
        if (SUCCEEDED(dlg->Show(g_hwnd))) {
            IShellItem* item = NULL;
            if (SUCCEEDED(dlg->GetResult(&item))) {
                PWSTR path = NULL;
                if (SUCCEEDED(item->GetDisplayName(SIGDN_FILESYSPATH, &path))) {
                    g_wsPath = path;
                    CoTaskMemFree(path);
                    SaveSettings();
                }
                item->Release();
            }
        }
        dlg->Release();
    }
}

static void AddAttachment(const std::wstring& path) {
    if (path.empty()) return;
    if (std::find(g_attachments.begin(), g_attachments.end(), path) != g_attachments.end())
        return;
    if (g_attachments.size() >= 10) { ShowToast(L"Up to 10 attachments at a time"); return; }
    g_attachments.push_back(path);
}

static bool IsImagePath(const std::wstring& p) {
    size_t d = p.find_last_of(L'.');
    if (d == std::wstring::npos) return false;
    std::wstring e = p.substr(d + 1);
    for (auto& c : e) c = towlower(c);
    return e == L"png" || e == L"jpg" || e == L"jpeg" || e == L"bmp" ||
           e == L"gif" || e == L"webp" || e == L"avif";
}

static int GetEncoderClsid(const WCHAR* format, CLSID* pClsid) {
    UINT num = 0, size = 0;
    GetImageEncodersSize(&num, &size);
    if (!size) return -1;
    std::vector<BYTE> buf(size);
    auto* info = (ImageCodecInfo*)buf.data();
    GetImageEncoders(num, size, info);
    for (UINT i = 0; i < num; ++i)
        if (!wcscmp(info[i].MimeType, format)) { *pClsid = info[i].Clsid; return (int)i; }
    return -1;
}

static std::wstring AttachmentDir() {
    CreateDirectoryW((g_root + L"\\data").c_str(), NULL);
    std::wstring d = g_root + L"\\data\\attachments";
    CreateDirectoryW(d.c_str(), NULL);
    return d;
}

static bool SaveBitmapPng(HBITMAP hb, const std::wstring& out) {
    Bitmap bmp(hb, NULL);
    CLSID png;
    if (GetEncoderClsid(L"image/png", &png) < 0) return false;
    return bmp.Save(out.c_str(), &png, NULL) == Ok;
}

// Ctrl+V: screenshots and copied images become files, copied files become
// attachments. Plain text is left to the edit control.
static int AttachClipboardFiles() {
    if (!g_hwnd || !OpenClipboard(g_hwnd)) return 0;
    int added = 0;
    if (IsClipboardFormatAvailable(CF_HDROP)) {
        if (HANDLE h = GetClipboardData(CF_HDROP)) {
            UINT n = DragQueryFileW((HDROP)h, 0xFFFFFFFF, NULL, 0);
            for (UINT i = 0; i < n; ++i) {
                wchar_t p[MAX_PATH];
                if (!DragQueryFileW((HDROP)h, i, p, MAX_PATH)) continue;
                DWORD at = GetFileAttributesW(p);
                if (at == INVALID_FILE_ATTRIBUTES || (at & FILE_ATTRIBUTE_DIRECTORY)) continue;
                size_t before = g_attachments.size();
                AddAttachment(p);
                if (g_attachments.size() != before) ++added;
            }
        }
    } else if (!IsClipboardFormatAvailable(CF_UNICODETEXT) &&
               IsClipboardFormatAvailable(CF_DIB)) {
        HBITMAP hb = NULL;
        bool temp = false;
        if (HANDLE hd = GetClipboardData(CF_DIB)) {
            auto* bi = (BITMAPINFO*)GlobalLock(hd);
            if (bi) {
                int bpp = bi->bmiHeader.biBitCount;
                size_t hdr = bi->bmiHeader.biSize +
                             (bpp <= 8 ? (size_t)(1 << bpp) * 4 : 0);
                void* bits = (BYTE*)bi + hdr;
                HDC dc = GetDC(NULL);
                hb = CreateDIBitmap(dc, &bi->bmiHeader, CBM_INIT, bits, bi, DIB_RGB_COLORS);
                ReleaseDC(NULL, dc);
                temp = hb != NULL;
                GlobalUnlock(hd);
            }
        }
        if (hb) {
            SYSTEMTIME st; GetLocalTime(&st);
            wchar_t name[96];
            swprintf(name, 96, L"\\paste-%04d%02d%02d-%02d%02d%02d-%03d.png",
                     st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute, st.wSecond,
                     st.wMilliseconds);
            std::wstring out = AttachmentDir() + name;
            size_t before = g_attachments.size();
            if (SaveBitmapPng(hb, out)) {
                AddAttachment(out);
                if (g_attachments.size() != before) ++added;
            } else {
                DeleteFileW(out.c_str());
            }
            if (temp) DeleteObject(hb);
        }
    }
    CloseClipboard();
    return added;
}

static void PickAttachments() {
    CoInitializeEx(NULL, COINIT_APARTMENTTHREADED);
    IFileOpenDialog* dlg = NULL;
    if (SUCCEEDED(CoCreateInstance(CLSID_FileOpenDialog, NULL, CLSCTX_INPROC_SERVER,
                                   IID_PPV_ARGS(&dlg)))) {
        DWORD opt; dlg->GetOptions(&opt);
        dlg->SetOptions(opt | FOS_ALLOWMULTISELECT | FOS_FORCEFILESYSTEM | FOS_FILEMUSTEXIST);
        COMDLG_FILTERSPEC types[] = {
            { L"Pictures and documents",
              L"*.png;*.jpg;*.jpeg;*.webp;*.bmp;*.gif;*.avif;*.pdf;*.docx;*.txt;*.md;*.csv;*.json" },
            { L"Pictures", L"*.png;*.jpg;*.jpeg;*.webp;*.bmp;*.gif;*.avif" },
            { L"All files", L"*.*" } };
        dlg->SetFileTypes(3, types);
        dlg->SetTitle(L"Attach pictures or files");
        if (SUCCEEDED(dlg->Show(g_hwnd))) {
            IShellItemArray* arr = NULL;
            if (SUCCEEDED(dlg->GetResults(&arr)) && arr) {
                DWORD n = 0; arr->GetCount(&n);
                for (DWORD i = 0; i < n; ++i) {
                    IShellItem* it = NULL;
                    if (SUCCEEDED(arr->GetItemAt(i, &it)) && it) {
                        PWSTR p = NULL;
                        if (SUCCEEDED(it->GetDisplayName(SIGDN_FILESYSPATH, &p))) {
                            AddAttachment(p);
                            CoTaskMemFree(p);
                        }
                        it->Release();
                    }
                }
                arr->Release();
            }
            if (!g_attachments.empty())
                ShowToast(std::to_wstring(g_attachments.size()) +
                          (g_attachments.size() == 1 ? L" file attached" : L" files attached"));
        }
        dlg->Release();
    }
    if (g_edit) SetFocus(g_edit);
    Refresh();
}

static void SendFromComposer() {
    if (g_busy) return;
    int len = GetWindowTextLengthW(g_edit);
    std::wstring text(len, 0);
    if (len) GetWindowTextW(g_edit, &text[0], len + 1);
    text = Trim(text);
    if (text.empty() && g_attachments.empty()) return;
    if (text.empty()) text = L"Look at the attached files and tell me what matters.";
    SetWindowTextW(g_edit, L"");
    RedrawWindow(g_edit, NULL, NULL, RDW_INVALIDATE | RDW_ERASE | RDW_UPDATENOW);
    AutoGrow();
    StartTurn(text);
}

// ---------------------------------------------------------------------------
// composer subclass (placeholder, keys)
// ---------------------------------------------------------------------------
static WNDPROC g_oldEdit = NULL;
static bool g_swallowReturn = false;
static LRESULT CALLBACK EditProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    switch (msg) {
    case WM_PAINT: {
        LRESULT r = CallWindowProcW(g_oldEdit, hwnd, msg, wp, lp);
        if (GetWindowTextLengthW(hwnd) == 0) {
            HDC d = GetDC(hwnd);
            RECT rc; GetClientRect(hwnd, &rc);
            rc.left += 4; rc.right -= 8; rc.top += 3;
            HFONT old = (HFONT)SelectObject(d, g_editFont);
            SetTextColor(d, C_DIM);
            SetBkMode(d, TRANSPARENT);
            DrawTextW(d, L"Ask for anything \x2014 download, tidy, research, control an app...",
                      -1, &rc, DT_LEFT | DT_TOP | DT_SINGLELINE | DT_END_ELLIPSIS);
            SelectObject(d, old);
            ReleaseDC(hwnd, d);
        }
        return r;
    }
    case WM_SETFOCUS:
    case WM_KILLFOCUS:
        InvalidateRect(hwnd, NULL, TRUE);
        InvalidateRect(g_hwnd, NULL, FALSE);
        return CallWindowProcW(g_oldEdit, hwnd, msg, wp, lp);
    case WM_KEYDOWN:
        if (wp == 'V' && (GetKeyState(VK_CONTROL) & 0x8000)) {
            int n = AttachClipboardFiles();
            if (n > 0) {
                ShowToast(std::to_wstring(g_attachments.size()) +
                          (g_attachments.size() == 1 ? L" file attached" : L" files attached"));
                Refresh();
                return 0;
            }
        }
        if (wp == VK_RETURN && !(GetKeyState(VK_SHIFT) & 0x8000)) {
            g_swallowReturn = true;
            SendFromComposer();
            return 0;
        }
        if (wp == VK_ESCAPE) { if (g_busy) StopAgent(); return 0; }
        if (wp == 'N' && (GetKeyState(VK_CONTROL) & 0x8000)) { NewChat(); return 0; }
        break;
    case WM_CHAR:
        if ((wp == L'\r' || wp == L'\n') && !(GetKeyState(VK_SHIFT) & 0x8000))
            return 0;
        if (wp == VK_TAB) return 0;
        if (!g_warmed && g_stdinWr && !g_busy) {
            g_warmed = true;
            SendCmd(L"{\"action\":\"prewarm\"}");
        }
        break;
    }
    return CallWindowProcW(g_oldEdit, hwnd, msg, wp, lp);
}

// ---------------------------------------------------------------------------
// layout
// ---------------------------------------------------------------------------
static void Relayout() {
    if (!g_hwnd || !g_edit) return;
    RECT er = EditRect();
    MoveWindow(g_edit, er.left, er.top, er.right - er.left, er.bottom - er.top, TRUE);
    // A layered child can keep stale pixels after a resize; re-applying the
    // layered attributes forces DWM to rebuild the surface.
    SetLayeredWindowAttributes(g_edit, 0, 255, LWA_ALPHA);
    RedrawWindow(g_edit, NULL, NULL, RDW_INVALIDATE | RDW_ERASE | RDW_UPDATENOW);
}

static int EditLineH() {
    if (!g_edit) return 21;
    HDC d = GetDC(g_edit);
    HFONT old = (HFONT)SelectObject(d, g_editFont);
    TEXTMETRICW tm{};
    GetTextMetricsW(d, &tm);
    SelectObject(d, old);
    ReleaseDC(g_edit, d);
    return (std::max)(18, (int)(tm.tmHeight + tm.tmExternalLeading));
}

// Grow the composer with the text until it hits the ceiling, then the edit scrolls.
static void AutoGrow() {
    if (!g_edit || !g_hwnd) return;
    int lines = GetWindowTextLengthW(g_edit)
                    ? (int)SendMessageW(g_edit, EM_GETLINECOUNT, 0, 0) : 1;
    if (lines < 1) lines = 1;
    int h = 58 + lines * EditLineH();
    h = (std::max)(92, (std::min)(236, h));
    if (h == g_compH) return;
    g_compH = h;
    Relayout();
    InvalidateRect(g_hwnd, NULL, FALSE);
}

// ---------------------------------------------------------------------------
// window
// ---------------------------------------------------------------------------
static void HitTest(POINT pt) {
    int id = HOT_NONE;
    for (auto& h : g_hits) if (PtInRect(&h.rc, pt)) id = h.id;
    if (id == HOT_NONE) for (auto& h : g_hitsSide) if (PtInRect(&h.rc, pt)) id = h.id;
    if (id != g_hover) { g_hover = id; Refresh(); }
}

static void OnClick(POINT pt) {
    int id = HOT_NONE;
    for (auto& h : g_hits) if (PtInRect(&h.rc, pt)) id = h.id;
    if (id == HOT_NONE) for (auto& h : g_hitsSide) if (PtInRect(&h.rc, pt)) id = h.id;

    if (g_modelMenu && id >= HOT_PICK_BASE && id < HOT_PICK_BASE + 200) {
        int i = id - HOT_PICK_BASE;
        if (i >= 0 && i < (int)g_models.size()) {
            g_model = g_models[i];
            SaveSettings();
            SendCmd(L"{\"action\":\"ping\"}");
        }
        g_modelMenu = false; Refresh(); return;
    }
    if (g_modelMenu) g_modelMenu = false;

    if (id >= HOT_COPY_TURN_BASE && id < HOT_COPY_TURN_BASE + 500) {
        int i = id - HOT_COPY_TURN_BASE;
        if (i < (int)g_turns.size() && !g_turns[i].user.empty()) {
            ShowToast(CopyToClipboard(g_turns[i].user) ? L"Prompt copied to clipboard"
                                                       : L"Could not copy to clipboard");
        }
        return;
    }
    if (id >= HOT_COPY_RECENT_BASE && id < HOT_COPY_RECENT_BASE + 20) {
        int i = id - HOT_COPY_RECENT_BASE;
        if (i < (int)g_recents.size()) {
            ShowToast(CopyToClipboard(g_recents[i]) ? L"Prompt copied to clipboard"
                                                    : L"Could not copy to clipboard");
        }
        return;
    }

    if (id == HOT_CLEAR_RECENTS) {
        if (g_recents.empty()) { ShowToast(L"No recent prompts to clear"); return; }
        g_recents.clear();
        SendCmd(L"{\"action\":\"clear_recents\"}");
        ShowToast(L"Recent prompts cleared");
        Refresh(); return;
    }
    if (id == HOT_ATTACH) { PickAttachments(); return; }
    if (id >= HOT_ATTACH_BASE && id < HOT_ATTACH_BASE + 40) {
        int i = id - HOT_ATTACH_BASE;
        if (i < (int)g_attachments.size()) {
            g_attachments.erase(g_attachments.begin() + i);
            ShowToast(L"Attachment removed");
            Refresh();
        }
        return;
    }
    if (id == HOT_NEW) { NewChat(); return; }
    if (id == HOT_WS) { PickFolder(); Refresh(); return; }
    if (id == HOT_MODEL) {
        g_modelMenu = !g_modelMenu;
        if (g_modelMenu) SendCmd(L"{\"action\":\"models\"}");
        Refresh(); return;
    }
    if (id == HOT_STUDY) {
        g_study = !g_study;
        SaveSettings();
        if (g_study) {
            if (g_busy) {
                StopAgent();
                g_study = true;
                g_studyStartPending = true;
                SaveSettings();
            } else {
                StartTurn(StudyPrompt(), true);
            }
        } else {
            g_studyStartPending = false;
            if (g_busy) StopAgent();
        }
        Refresh(); return;
    }
    if (id >= HOT_RECENT_BASE && id < HOT_RECENT_BASE + 20) {
        int i = id - HOT_RECENT_BASE;
        if (i < (int)g_recents.size()) {
            SetWindowTextW(g_edit, g_recents[i].c_str());
            SendMessageW(g_edit, EM_SETSEL, 0, -1);
            SetFocus(g_edit);
            AutoGrow();
        }
        return;
    }
    if (id >= HOT_SUGGEST_BASE && id < HOT_SUGGEST_BASE + 20) {
        int i = id - HOT_SUGGEST_BASE;
        if (i >= 0 && i < 3) { SetFocus(g_edit); StartTurn(IDEAS[i].prompt); }
        return;
    }
    if (id == HOT_SEND) { SendFromComposer(); return; }
    if (id == HOT_STOP) { StopAgent(); return; }
    if (id >= HOT_TURN_BASE && id < HOT_TURN_BASE + 500) {
        int i = id - HOT_TURN_BASE;
        if (i < (int)g_turns.size()) { g_turns[i].expanded = !g_turns[i].expanded; Refresh(); }
        return;
    }
    if (id >= HOT_STEP_BASE && id < HOT_STEP_BASE + 50000) {
        int ti = (id - HOT_STEP_BASE) / 100, si = (id - HOT_STEP_BASE) % 100;
        if (ti < (int)g_turns.size() && si < (int)g_turns[ti].steps.size()) {
            g_turns[ti].steps[si].expanded = !g_turns[ti].steps[si].expanded;
            Refresh();
        }
        return;
    }
    SetFocus(g_edit);
    Refresh();
}

static LRESULT CALLBACK WndProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    switch (msg) {
    case WM_CREATE: {
        BOOL dark = TRUE;
        DwmSetWindowAttribute(hwnd, 20, &dark, sizeof(dark));
        COLORREF cap = C_APP, bord = RGB(40, 36, 30);
        DwmSetWindowAttribute(hwnd, 35, &cap, sizeof(cap));
        DwmSetWindowAttribute(hwnd, 34, &bord, sizeof(bord));
        SendMessageW(hwnd, WM_SETICON, ICON_BIG, (LPARAM)LoadAppIcon(256));
        SendMessageW(hwnd, WM_SETICON, ICON_SMALL, (LPARAM)LoadAppIcon(32));

        g_brApp = CreateSolidBrush(C_APP);
        g_brEdit = CreateSolidBrush(C_PANEL);

        auto MK = [](int h, int wgt, const wchar_t* face, bool ital = false) {
            return CreateFontW(-h, 0, 0, 0, wgt, ital, 0, 0, DEFAULT_CHARSET,
                               OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                               CLEARTYPE_QUALITY, DEFAULT_PITCH | FF_DONTCARE, face);
        };
        const wchar_t* UI = L"Segoe UI Variable Text";
        const wchar_t* DP = L"Segoe UI Variable Display";
        F_H1    = MK(31, FW_SEMIBOLD, DP);
        F_H2    = MK(17, FW_SEMIBOLD, DP);
        F_TITLE = MK(22, FW_SEMIBOLD, DP);
        F_UI    = MK(14, FW_NORMAL, UI);
        F_UIB   = MK(14, FW_SEMIBOLD, UI);
        F_SMALL = MK(12, FW_NORMAL, UI);
        F_SMALLB= MK(12, FW_SEMIBOLD, UI);
        F_LABEL = MK(10, FW_BOLD, UI);
        F_CHAT  = MK(15, FW_NORMAL, UI);
        F_ITALIC= MK(15, FW_NORMAL, UI, true);
        F_MONO  = MK(13, FW_NORMAL, L"Cascadia Mono");
        F_MONOS = MK(12, FW_NORMAL, L"Cascadia Mono");
        F_GLYPH = MK(16, FW_NORMAL, L"Segoe MDL2 Assets");
        g_editFont = MK(15, FW_NORMAL, UI);

        g_edit = CreateWindowExW(0, L"EDIT", L"",
            WS_CHILD | WS_VISIBLE | ES_MULTILINE | ES_AUTOVSCROLL | ES_WANTRETURN,
            0, 0, 0, 0, hwnd, (HMENU)ID_COMPOSER, NULL, NULL);
        SetWindowTheme(g_edit, L"", L"");
        // Child windows on this DWM build composite with alpha 0; forcing an opaque
        // layered surface stops the white wash and lets the brush colour show exactly.
        SetWindowLongPtrW(g_edit, GWL_EXSTYLE, GetWindowLongPtrW(g_edit, GWL_EXSTYLE) | WS_EX_LAYERED);
        SetLayeredWindowAttributes(g_edit, 0, 255, LWA_ALPHA);
        SendMessageW(g_edit, WM_SETFONT, (WPARAM)g_editFont, TRUE);
        SendMessageW(g_edit, EM_SETMARGINS, EC_LEFTMARGIN | EC_RIGHTMARGIN, MAKELPARAM(2, 2));
        g_oldEdit = (WNDPROC)SetWindowLongPtrW(g_edit, GWLP_WNDPROC, (LONG_PTR)EditProc);

        SetTimer(hwnd, 1, 16, NULL);
        AddTrayIcon(hwnd);
        DragAcceptFiles(hwnd, TRUE);
        LoadUiSettings();
        NewChat();
        Boot();
        SetFocus(g_edit);
        return 0;
    }
    case WM_CLOSE:
        // Closing while the agent is mid-run keeps it alive in the tray instead of
        // orphaning a background process with no way to stop it.
        if (g_busy && g_trayAdded) { HideToTray(hwnd); return 0; }
        DestroyWindow(hwnd);
        return 0;
    case WM_TRAYICON: {
        if (lp == WM_LBUTTONUP || lp == WM_LBUTTONDBLCLK) {
            ShowFromTray(hwnd);
        } else if (lp == WM_RBUTTONUP || lp == WM_CONTEXTMENU) {
            POINT pt; GetCursorPos(&pt);
            HMENU m = CreatePopupMenu();
            AppendMenuW(m, MF_STRING, 1, L"Open Anvil Fletch");
            AppendMenuW(m, MF_STRING | (g_busy ? MF_ENABLED : MF_GRAYED), 2,
                        g_busy ? L"Stop the running agent" : L"Agent idle");
            AppendMenuW(m, MF_SEPARATOR, 0, NULL);
            AppendMenuW(m, MF_STRING, 3, L"Quit (kills the agent)");
            SetForegroundWindow(hwnd);
            int cmd = TrackPopupMenu(m, TPM_RETURNCMD | TPM_RIGHTBUTTON,
                                     pt.x, pt.y, 0, hwnd, NULL);
            DestroyMenu(m);
            PostMessageW(hwnd, WM_NULL, 0, 0);
            if (cmd == 1) ShowFromTray(hwnd);
            else if (cmd == 2) { StopAgent(); ShowFromTray(hwnd); }
            else if (cmd == 3) DestroyWindow(hwnd);
        }
        return 0;
    }
    case WM_GETMINMAXINFO: {
        MINMAXINFO* m = (MINMAXINFO*)lp;
        m->ptMinTrackSize = { 1020, 660 };
        return 0;
    }
    case WM_SIZE: AutoGrow(); Relayout(); g_frameValid = false; InvalidateRect(hwnd, NULL, FALSE); return 0;
    case WM_ERASEBKGND: return 1;
    case WM_PAINT: OnPaint(hwnd); return 0;
    case WM_COMMAND:
        if (LOWORD(wp) == ID_COMPOSER && HIWORD(wp) == EN_CHANGE)
            PostMessageW(hwnd, WM_AUTOGROW, 0, 0);
        return 0;
    case WM_AUTOGROW: AutoGrow(); return 0;
    case WM_STUDY_START:
        if (g_study && !g_busy) StartTurn(StudyPrompt(), true);
        return 0;
    case WM_CTLCOLOREDIT: {
        HDC d = (HDC)wp;
        SetTextColor(d, C_TEXT);
        SetBkColor(d, C_PANEL);
        return (LRESULT)g_brEdit;
    }
    case WM_CTLCOLORSTATIC: {
        HDC d = (HDC)wp; SetBkMode(d, TRANSPARENT); return (LRESULT)g_brEdit;
    }
    case WM_MOUSEMOVE: {
        POINT pt{ GET_X_LPARAM(lp), GET_Y_LPARAM(lp) };
        HitTest(pt);
        TRACKMOUSEEVENT tme{ sizeof(tme), TME_LEAVE, hwnd, 0 };
        TrackMouseEvent(&tme);
        return 0;
    }
    case WM_MOUSELEAVE:
        if (g_hover != HOT_NONE) { g_hover = HOT_NONE; Refresh(); }
        return 0;
    case WM_LBUTTONDOWN:
        OnClick(POINT{ GET_X_LPARAM(lp), GET_Y_LPARAM(lp) });
        return 0;
    case WM_MOUSEWHEEL: {
        RECT v = ViewRect();
        int vh = v.bottom - v.top;
        int delta = GET_WHEEL_DELTA_WPARAM(wp);
        int maxS = (std::max)(0, g_contentH - vh + 12);
        g_scroll -= delta / WHEEL_DELTA * 96;
        g_scroll = (std::max)(0, (std::min)(maxS, g_scroll));
        g_follow = (g_scroll >= maxS - 4);
        Refresh();
        return 0;
    }
    case WM_KEYDOWN:
        if (wp == 'N' && (GetKeyState(VK_CONTROL) & 0x8000)) { NewChat(); return 0; }
        if (wp == VK_ESCAPE && g_busy) { StopAgent(); return 0; }
        if (wp == VK_TAB) { SetFocus(g_edit); return 0; }
        break;
    case WM_DROPFILES: {
        HDROP hd = (HDROP)wp;
        UINT n = DragQueryFileW(hd, 0xFFFFFFFF, NULL, 0);
        bool skippedFolder = false;
        for (UINT i = 0; i < n; ++i) {
            wchar_t p[MAX_PATH];
            if (!DragQueryFileW(hd, i, p, MAX_PATH)) continue;
            DWORD attr = GetFileAttributesW(p);
            if (attr == INVALID_FILE_ATTRIBUTES) continue;
            if (attr & FILE_ATTRIBUTE_DIRECTORY) { skippedFolder = true; continue; }
            AddAttachment(p);
        }
        DragFinish(hd);
        if (!g_attachments.empty())
            ShowToast(std::to_wstring(g_attachments.size()) +
                      (g_attachments.size() == 1 ? L" file attached" : L" files attached"));
        else if (skippedFolder)
            ShowToast(L"Drop files, not folders");
        if (g_edit) SetFocus(g_edit);
        Refresh();
        return 0;
    }
    case WM_TIMER: {
        ULONGLONG now = GetTickCount64();
        if (g_prog.active && now >= g_prog.until) g_prog.active = false;
        if (!IsWindowVisible(hwnd) || IsIconic(hwnd)) return 0;
        // A slow full frame earns a longer gap; animation-only frames stay fast
        // because they only blit a few 24px squares.
        ULONGLONG gap = 16;
        if (g_paintCostMs > 12) gap = 33;
        if (g_paintCostMs > 40) gap = 66;
        // Nothing on screen changes while the model is merely thinking, so a
        // repaint is only worth it when an event arrived, a spinner is on
        // screen, or a progress bar/toast is animating.
        bool want = LiveSpinner() || g_prog.active || g_paintAgain ||
                    (g_uiRev != g_lastPaintRev) ||
                    (g_toastUntil && now < g_toastUntil + 250);
        if (want) {
            bool animOnly = CanAnimateOnly();
            ULONGLONG useGap = animOnly ? 16 : gap;
            if (now - g_lastPaintAt >= useGap) {
                g_paintAgain = false;
                g_lastPaintAt = now;
                g_lastPaintRev = g_uiRev;
                if (animOnly) DrawAnimationFrame(hwnd);
                else InvalidateRect(hwnd, NULL, FALSE);
            }
        }
        return 0;
    }
    case WM_AGENT_LINE: {
        g_linePosted = false;
        unsigned long long rev = g_uiRev;
        for (;;) {
            std::wstring line;
            {
                std::lock_guard<std::mutex> lk(g_qmu);
                if (g_q.empty()) break;
                line = g_q.front(); g_q.pop_front();
            }
            if (line.empty() || line[0] != L'{') continue;
            JV v = ParseJson(line);
            if (!v.at(L"e")) {
                Step s; s.kind = Step::Note; s.text = line.substr(0, 200); s.ok = false;
                if (!g_turns.empty()) g_turns.back().steps.push_back(s);
                continue;
            }
            OnEvent(v);
        }
        // Lines that raced in while this message was being handled need a new
        // wake-up; otherwise the queue would sit there until the next event.
        {
            std::lock_guard<std::mutex> lk(g_qmu);
            if (!g_q.empty() && !g_linePosted.exchange(true))
                PostMessageW(hwnd, WM_AGENT_LINE, 0, 0);
        }
        // While busy the timer paints on its own clock; idle events paint now.
        if (g_uiRev != rev && !g_busy) Refresh();
        return 0;
    }
    case WM_DESTROY:
        if (g_trayAdded) {
            Shell_NotifyIconW(NIM_DELETE, &g_nid);
            g_trayAdded = false;
        }
        if (g_proc) TerminateProcess(g_proc, 0);
        KillTimer(hwnd, 1);
        if (g_frameDC) { DeleteDC(g_frameDC); g_frameDC = NULL; }
        if (g_frameBmp) { DeleteObject(g_frameBmp); g_frameBmp = NULL; }
        g_frameValid = false;
        PostQuitMessage(0);
        return 0;
    }
    return DefWindowProcW(hwnd, msg, wp, lp);
}

int WINAPI wWinMain(HINSTANCE inst, HINSTANCE, LPWSTR, int show) {
    SetProcessDPIAware();
    GdiplusStartupInput gsi;
    ULONG_PTR token;
    GdiplusStartup(&token, &gsi, NULL);
    CoInitializeEx(NULL, COINIT_APARTMENTTHREADED);

    INITCOMMONCONTROLSEX ic{ sizeof(ic), ICC_STANDARD_CLASSES };
    InitCommonControlsEx(&ic);

    g_root = ExeDir();

    WNDCLASSEXW wc{ sizeof(wc) };
    wc.style = CS_HREDRAW | CS_VREDRAW;
    wc.lpfnWndProc = WndProc;
    wc.hInstance = inst;
    wc.lpszClassName = L"AnvilFletchWindow";
    wc.hCursor = LoadCursor(NULL, IDC_ARROW);
    wc.hbrBackground = NULL;
    wc.hIcon = LoadAppIcon(32);
    wc.hIconSm = LoadAppIcon(16);
    RegisterClassExW(&wc);

    RECT desk{ 0, 0, 0, 0 };
    SystemParametersInfo(SPI_GETWORKAREA, 0, &desk, 0);
    int ww = (std::min)(1380, (int)(desk.right - desk.left) - 70);
    int wh = (std::min)(900, (int)(desk.bottom - desk.top) - 60);

    HWND hwnd = CreateWindowExW(0, L"AnvilFletchWindow", L"Anvil Fletch",
        WS_OVERLAPPEDWINDOW | WS_CLIPCHILDREN,
        (desk.left + desk.right) / 2 - ww / 2, (desk.top + desk.bottom) / 2 - wh / 2,
        ww, wh, NULL, NULL, inst, NULL);
    g_hwnd = hwnd;
    ShowWindow(hwnd, show);
    UpdateWindow(hwnd);

    MSG m;
    while (GetMessage(&m, NULL, 0, 0)) {
        if (m.message == WM_KEYDOWN || m.message == WM_CHAR) {
            if (m.hwnd != g_edit && g_edit) {
                SendMessageW(g_edit, m.message, m.wParam, m.lParam);
                SetForegroundWindow(hwnd);
                continue;
            }
        }
        TranslateMessage(&m);
        DispatchMessage(&m);
    }
    GdiplusShutdown(token);
    return (int)m.wParam;
}
