# Project Fjord-Radar: DFS Channel Mapping & Automation

## 1. Project Context & Environment
**Location:** Asker, Norway. Situated ~6 km between Drammensfjorden and Oslofjorden.
**Proximity to RF Sources:**
*   **Weather Radar:** 8-12 km away, operating at 5.62 GHz (Ch 124) and 5.64 GHz (Ch 128).
*   **Aviation:** Avinor area controllers located a few km away (no localized radar confirmed).
*   **Maritime:** Heavy commercial shipping traffic in adjacent fjords.

**Hardware Setup:**
*   **Main Router:** UniFi UDR7 (B17) (Operating on Ch 36 @ 160MHz, utilizing non-DFS slots).
*   **Target AP:** UniFi AC HD (Living room, experiencing frequent DFS radar hits).
*   **Other APs in Logs:** B17, AC LR.

## 2. The Objective
Map the local 5 GHz DFS (Dynamic Frequency Selection) spectrum over several months to identify the cleanest frequencies. The ultimate goal is to find contiguous, uninterrupted 20MHz blocks to safely bind together for stable 40MHz or 80MHz channel operation without being forced off by radar events.

## 3. Initial Log Analysis
Syslog snippets revealed direct radar hits across multiple DFS channels:
*   **Channel 100:** Frequent hits (AC HD)
*   **Channel 108:** Intermittent hits (AC HD)
*   **Channel 116:** Intermittent hits (AC HD, B17, AC LR)
*   **Channel 124:** Direct hits (AC HD)

## 4. RF Environment Analysis
The physical geography creates a unique RF challenge, explaining why radar is detected despite distance and line-of-sight obstructions:

*   **Weather Radar (Direct Hits):** The local weather radar operates at 5620 MHz and 5640 MHz. In Wi-Fi standards, these map exactly to the center frequencies of **Channel 124** and **Channel 128**. The log hits on Ch 124 are direct strikes from this array.
*   **Marine Radar (Fjord Traffic):** The hits on Channels 100, 108, and 116 (5.470 GHz – 5.650 GHz band) align with maritime radiolocation systems.
*   **Atmospheric Ducting:** Because the location is sandwiched between two fjords, temperature inversions (cold/moist marine air trapped below warmer/drier air) create an "atmospheric duct." This causes 5 GHz microwave signals from ships and distant radars to bend along the earth's curvature rather than escaping into the atmosphere, directly sweeping the local Wi-Fi environment.

## 5. Methodology Correction: MTBD vs. Pings Per Day
**Initial Idea:** A script to set the AC HD to a new 20MHz channel every day at 00:00 to graph "pings per day."
**The Flaw:** When an AP detects radar, it immediately vacates the channel. If a script sets Ch 100 at 00:00, and radar hits at 01:00, the AP abandons the channel for the next 23 hours. The channel does not get equal listening time, making daily frequency comparisons mathematically invalid.
**The Solution:** Measure **Mean Time Between Detections (MTBD)**.
*   Formula: `Total Hours Active on a Specific Channel / Number of Detections`.
*   Strategy: Set the AP to a single 20MHz channel and leave it there until a radar hit forces it off. Measure the exact uptime.

## 6. Channel Bonding Constraints (Why 20MHz Testing is Critical)
To eventually run wider channels, the underlying 20MHz blocks must be mapped first.
*   **40MHz Rule:** Binds two 20MHz channels (e.g., Ch 100 + 104).
*   **80MHz Rule:** Binds four 20MHz channels (e.g., Ch 100 + 104 + 108 + 112).
*   **The Catch:** If *any single 20MHz sub-channel* detects radar, the AP drops the entire 40MHz or 80MHz block. Therefore, a stable 40MHz channel requires finding two *contiguous* clean channels; an 80MHz requires four.

## 7. Developer Implementation Plan
This repo implements that plan. See [`../README.md`](../README.md) for usage
and [`SETUP.md`](SETUP.md) for the UDR-side configuration.
