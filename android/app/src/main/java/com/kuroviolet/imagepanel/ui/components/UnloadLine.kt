package com.kuroviolet.imagepanel.ui.components

import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import com.kuroviolet.imagepanel.net.b
import com.kuroviolet.imagepanel.net.i
import com.kuroviolet.imagepanel.net.s
import kotlinx.coroutines.delay
import kotlinx.serialization.json.JsonObject

/** "1 min 40 s": whole seconds, to the nearest ten. */
fun countdown(seconds: Int): String {
    val s = (Math.round(seconds.coerceAtLeast(0) / 10.0) * 10).toInt()
    val h = s / 3600
    val m = s % 3600 / 60
    val r = s % 60
    return when {
        h > 0 -> if (m > 0) "$h h $m min" else "$h h"
        m > 0 -> if (r > 0) "$m min $r s" else "$m min"
        else -> "$r s"
    }
}

/**
 * When a model lets go of the graphics card, from the status' `unload` object (`loaded`, `idleSeconds`, `unloadInSeconds`,
 * `rule` = after-work / interactive / server, `busy`): "Models loaded · unloads in ~1 min 40 s if nothing new" or
 * "Not loaded · GPU memory free". `elapsed` is the number of seconds since the status was read.
 */
fun unloadText(u: JsonObject, noun: String, elapsed: Int = 0): String {
    if (u.b("loaded") != true) return "Not loaded · GPU memory free"
    if (u.b("busy") == true) return "$noun loaded · working"
    if (u.i("idleSeconds") == null) return "$noun loading…"
    val left = u.i("unloadInSeconds") ?: return "$noun loaded"
    val remaining = (left - elapsed).coerceAtLeast(0)
    val soon = if (remaining < 5) "any moment now" else "in ~${countdown(remaining)}"
    return when (u.s("rule")) {
        "after-work" -> "$noun loaded · unloads $soon if nothing new"
        "interactive" -> "$noun loaded · kept for your last use · unloads $soon if unused"
        else -> "$noun loaded · unloads by itself $soon if unused"
    }
}

/** [unloadText] as a hint line that counts down every second between two status reads. */
@Composable
fun UnloadLine(u: JsonObject, noun: String) {
    var elapsed by remember(u) { mutableIntStateOf(0) }
    LaunchedEffect(u) {
        while (true) {
            delay(1000)
            elapsed++
        }
    }
    Hint(unloadText(u, noun, elapsed))
}
