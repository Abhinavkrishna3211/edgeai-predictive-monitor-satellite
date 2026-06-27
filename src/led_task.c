/*
 * led_task.c — Timer-driven LED state machine (single LED or RGB LED).
 *
 * Tick = 100 ms.  All patterns are rhythm-based so each state is
 * distinguishable by counting taps and observing gaps — not by estimating
 * frequency (humans are bad at estimating Hz, good at counting 1-2-3).
 *
 * Pattern table (tick wraps at LCM(10,20,30,2) = 60):
 *
 *  LED_BOOT          always ON
 *  LED_WIFI_CONN     ON at ticks 0,2,4  → OFF for ticks 5-9  (3 taps / 1 s)
 *  LED_TCP_CONN      ON for ticks  0-9  → OFF for ticks 10-19 (0.5 Hz)
 *  LED_CALIBRATING   ON at ticks 0,2    → OFF for ticks 3-19  (2 taps / 2 s)
 *  LED_OK            ON at tick   0     → OFF for ticks  1-29  (blip / 3 s)
 *  LED_WARN          ON for ticks 0-4   → OFF for ticks  5-9   (1 Hz)
 *  LED_FAULT         ON for even ticks                          (5 Hz strobe)
 *
 * RGB upgrade: define EPM_LED_RGB=1 in epm_config.h and set LED_PIN_R/G/B.
 * The same tick patterns apply; each state drives a specific colour instead
 * of brightness.  The single-LED code path is unchanged.
 */

#include "led_task.h"
#include "epm_config.h"

#include "driver/gpio.h"
#include "esp_timer.h"
#include "esp_log.h"

static const char *TAG = "led_task";

/* ─── Single built-in LED (active-low) ───────────────────────────────────── */

#ifndef EPM_LED_RGB
#define EPM_LED_RGB 0
#endif

#if !EPM_LED_RGB

#define LED_ON()   gpio_set_level((gpio_num_t)ALERT_LED_PIN, 0)  /* active-low */
#define LED_OFF()  gpio_set_level((gpio_num_t)ALERT_LED_PIN, 1)

static void _gpio_init_single(void)
{
    gpio_config_t cfg = {
        .pin_bit_mask  = (1ULL << ALERT_LED_PIN),
        .mode          = GPIO_MODE_OUTPUT,
        .pull_up_en    = GPIO_PULLUP_DISABLE,
        .pull_down_en  = GPIO_PULLDOWN_DISABLE,
        .intr_type     = GPIO_INTR_DISABLE,
    };
    gpio_config(&cfg);
    LED_ON();   /* solid on during boot */
}

static void _apply_single(int on)
{
    if (on) { LED_ON(); } else { LED_OFF(); }
}

#else  /* EPM_LED_RGB — external common-cathode RGB LED ─────────────────── */

/* Colour table indexed by led_state_t */
static const uint8_t _rgb_table[7][3] = {
    /* R,   G,   B */
    {255, 255, 255},   /* BOOT          — white   */
    {  0,   0, 255},   /* WIFI_CONN     — blue    */
    {  0, 200, 255},   /* TCP_CONN      — cyan    */
    {160,   0, 255},   /* CALIBRATING   — purple  */
    {  0, 200,   0},   /* OK            — green   */
    {255, 170,   0},   /* WARN          — yellow  */
    {255,   0,   0},   /* FAULT         — red     */
};

static void _gpio_init_rgb(void)
{
    uint64_t mask = (1ULL << LED_PIN_R) | (1ULL << LED_PIN_G) | (1ULL << LED_PIN_B);
    gpio_config_t cfg = {
        .pin_bit_mask  = mask,
        .mode          = GPIO_MODE_OUTPUT,
        .pull_up_en    = GPIO_PULLUP_DISABLE,
        .pull_down_en  = GPIO_PULLDOWN_DISABLE,
        .intr_type     = GPIO_INTR_DISABLE,
    };
    gpio_config(&cfg);
    /* Boot = white */
    gpio_set_level((gpio_num_t)LED_PIN_R, 1);
    gpio_set_level((gpio_num_t)LED_PIN_G, 1);
    gpio_set_level((gpio_num_t)LED_PIN_B, 1);
}

/* Simple on/off drive for RGB (no PWM yet — brightness comes from the pattern
 * tick.  Add LEDC/PWM here later for smooth colour mixing if desired). */
static led_state_t _rgb_cur_state = LED_BOOT;

static void _apply_rgb(led_state_t state, int on)
{
    _rgb_cur_state = state;
    if (on) {
        const uint8_t *c = _rgb_table[state];
        gpio_set_level((gpio_num_t)LED_PIN_R, c[0] > 0 ? 1 : 0);
        gpio_set_level((gpio_num_t)LED_PIN_G, c[1] > 0 ? 1 : 0);
        gpio_set_level((gpio_num_t)LED_PIN_B, c[2] > 0 ? 1 : 0);
    } else {
        gpio_set_level((gpio_num_t)LED_PIN_R, 0);
        gpio_set_level((gpio_num_t)LED_PIN_G, 0);
        gpio_set_level((gpio_num_t)LED_PIN_B, 0);
    }
}

#endif  /* EPM_LED_RGB */

/* ─── Shared state machine ────────────────────────────────────────────────── */

/* Atomic access — safe for cross-core write (wifi_task on CPU0) /
 * read (esp_timer dispatch on either core). */
static led_state_t s_state = LED_BOOT;

static void led_timer_cb(void *arg)
{
    static uint8_t tick = 0;
    led_state_t state = __atomic_load_n(&s_state, __ATOMIC_RELAXED);

    int on;
    uint8_t t;

    switch (state) {

        case LED_BOOT:
            on = 1;
            break;

        case LED_WIFI_CONN:
            /*
             * 3 quick taps, then 700 ms dark — repeats every 1 s (10 ticks).
             * Reads as "tap-tap-tap … pause … tap-tap-tap".
             * Clearly different from TCP (slow blink) and FAULT (no pause).
             */
            t  = tick % 10;
            on = (t == 0 || t == 2 || t == 4);
            break;

        case LED_TCP_CONN:
            /*
             * Slow 0.5 Hz equal blink: 1 s ON / 1 s OFF (20-tick period).
             * Reads as "long-on … long-off".  Much slower than WARN (1 Hz),
             * which makes them easy to tell apart.
             */
            on = (tick % 20) < 10;
            break;

        case LED_CALIBRATING:
            /*
             * 2 quick taps then 1.8 s dark — repeats every 2 s (20 ticks).
             * Reads as "tap-tap … long pause".
             * Different from WIFI (3 taps, 0.7 s pause) — you can count the taps.
             */
            t  = tick % 20;
            on = (t == 0 || t == 2);
            break;

        case LED_OK:
            /*
             * Single 100 ms blip every 3 s — LED is almost always OFF.
             * The rarity of the flash is the signal: "calm, healthy, don't worry".
             */
            on = (tick % 30) == 0;
            break;

        case LED_WARN:
            /*
             * Steady 1 Hz blink: 500 ms ON / 500 ms OFF (10-tick period).
             * Reads like a car hazard light — "pay attention, something's up".
             * Clearly faster than TCP (0.5 Hz) and clearly slower than FAULT (5 Hz).
             */
            on = (tick % 10) < 5;
            break;

        case LED_FAULT:
            /*
             * Continuous 5 Hz strobe: 100 ms ON / 100 ms OFF (2-tick period).
             * You cannot count individual flashes — it reads as pure ALARM.
             * Unmistakably different from every other state.
             */
            on = (tick % 2) == 0;
            break;

        default:
            on = 0;
            break;
    }

#if EPM_LED_RGB
    _apply_rgb(state, on);
#else
    _apply_single(on);
#endif

    /* LCM(10, 20, 20, 30, 10, 2) = 60 — all patterns stay phase-aligned */
    if (++tick >= 60) tick = 0;
}

/* ─── Public API ──────────────────────────────────────────────────────────── */

void led_set_state(led_state_t state)
{
    if ((unsigned)state > (unsigned)LED_FAULT) return;
    __atomic_store_n(&s_state, state, __ATOMIC_RELAXED);
}

/* Module-level timer handle — prevents a second call to led_task_start()
 * from creating a duplicate timer that doubles every pattern's flash rate. */
static esp_timer_handle_t s_led_timer = NULL;

void led_task_start(void)
{
    if (s_led_timer != NULL) {
        ESP_LOGW(TAG, "led_task_start() called twice — ignoring");
        return;
    }

#if EPM_LED_RGB
    _gpio_init_rgb();
    ESP_LOGI(TAG, "LED task started (RGB GPIO R=%d G=%d B=%d, 7-state)",
             LED_PIN_R, LED_PIN_G, LED_PIN_B);
#else
    _gpio_init_single();
    ESP_LOGI(TAG, "LED task started (GPIO%d active-low, 7-state rhythm patterns)",
             ALERT_LED_PIN);
#endif

    const esp_timer_create_args_t timer_args = {
        .callback = led_timer_cb,
        .arg      = NULL,
        .name     = "led_timer",
    };
    ESP_ERROR_CHECK(esp_timer_create(&timer_args, &s_led_timer));
    ESP_ERROR_CHECK(esp_timer_start_periodic(s_led_timer, 100 * 1000));   /* 100 ms */
}
