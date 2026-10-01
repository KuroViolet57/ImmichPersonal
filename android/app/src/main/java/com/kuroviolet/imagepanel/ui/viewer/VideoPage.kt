package com.kuroviolet.imagepanel.ui.viewer

import androidx.annotation.OptIn
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.foundation.background
import kotlin.math.sign
import kotlin.math.pow
import kotlin.math.abs
import androidx.compose.foundation.gestures.detectDragGesturesAfterLongPress
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.layout.safeDrawing
import androidx.compose.foundation.layout.only
import androidx.compose.foundation.layout.WindowInsetsSides
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.VolumeOff
import androidx.compose.material.icons.automirrored.filled.VolumeUp
import androidx.compose.material.icons.filled.Forward10
import androidx.compose.material.icons.filled.Fullscreen
import androidx.compose.material.icons.filled.Pause
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Repeat
import androidx.compose.material.icons.filled.RepeatOn
import androidx.compose.material.icons.filled.Replay10
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.Slider
import androidx.compose.material3.SliderDefaults
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.okhttp.OkHttpDataSource
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.ui.AspectRatioFrameLayout
import androidx.media3.ui.PlayerView
import coil3.compose.AsyncImage
import com.kuroviolet.imagepanel.Graph
import com.kuroviolet.imagepanel.diag.Diag
import com.kuroviolet.imagepanel.model.Asset
import kotlinx.coroutines.delay

private val SPEEDS = listOf(1f, 1.25f, 1.5f, 2f, 0.5f)

fun fmtTime(ms: Long): String {
    if (ms <= 0) return "0:00"
    val t = ms / 1000
    val h = t / 3600
    val m = (t % 3600) / 60
    val s = t % 60
    return if (h > 0) "%d:%02d:%02d".format(h, m, s) else "%d:%02d".format(m, s)
}

/**
 * A video with its own controls and gestures (like the panel's player):
 * tap = show/hide controls · double-tap left/right = −10 s / +10 s (keeps adding) · double-tap middle =
 * play/pause · hold = 2× while held · swipe left/right = previous/next item (the pager).
 */
@OptIn(UnstableApi::class)
@Composable
fun VideoPage(
    asset: Asset,
    active: Boolean,
    chrome: Boolean,
    setChrome: (Boolean) -> Unit,
    onFullscreen: () -> Unit,
) {
    if (!active) {
        Box(Modifier.fillMaxSize().background(Color.Black), contentAlignment = Alignment.Center) {
            AsyncImage(model = Graph.api.thumbUrl(asset.id, "preview"), contentDescription = asset.name,
                contentScale = ContentScale.Fit, modifier = Modifier.fillMaxSize())
            Icon(Icons.Filled.PlayArrow, contentDescription = null, tint = Color.White, modifier = Modifier.size(64.dp))
        }
        return
    }
    val context = LocalContext.current
    var error by remember(asset.id) { mutableStateOf<String?>(null) }
    val player = remember(asset.id) {
        ExoPlayer.Builder(context)
            .setMediaSourceFactory(DefaultMediaSourceFactory(OkHttpDataSource.Factory(Graph.http)))
            .build()
            .apply {
                setMediaItem(MediaItem.fromUri(Graph.api.videoUrl(asset.id)))
                prepare()
                playWhenReady = true
            }
    }
    var position by remember { mutableLongStateOf(0L) }
    var duration by remember { mutableLongStateOf(0L) }
    var playing by remember { mutableStateOf(true) }
    var buffering by remember { mutableStateOf(true) }
    var speed by remember { mutableFloatStateOf(1f) }
    var loop by remember { mutableStateOf(false) }
    var muted by remember { mutableStateOf(false) }
    var dragging by remember { mutableStateOf(false) }
    var dragValue by remember { mutableFloatStateOf(0f) }
    var holding by remember { mutableStateOf(false) }
    var flash by remember { mutableStateOf<Pair<String, Int>?>(null) }   // text, side (-1 left, 1 right, 0 middle)
    var flashKey by remember { mutableIntStateOf(0) }
    var seekSum by remember { mutableIntStateOf(0) }
    var poke by remember { mutableIntStateOf(0) }
    var scrub by remember { mutableStateOf<Long?>(null) }       // press, hold, then slide: where it will seek to
    var scrubFrom by remember { mutableLongStateOf(0L) }

    DisposableEffect(player) {
        val listener = object : Player.Listener {
            override fun onPlayerError(e: PlaybackException) {
                Diag.w("player", "${asset.name} (${asset.id}): ${e.errorCodeName}: ${e.message}")
                error = "Can't play this video (${e.errorCodeName})."
            }
            override fun onIsPlayingChanged(isPlaying: Boolean) { playing = isPlaying }
            override fun onPlaybackStateChanged(state: Int) { buffering = state == Player.STATE_BUFFERING }
        }
        player.addListener(listener)
        onDispose {
            player.removeListener(listener)
            player.release()
        }
    }
    LaunchedEffect(player) {
        while (true) {
            if (!dragging) position = player.currentPosition
            duration = player.duration.coerceAtLeast(0L)
            delay(200)
        }
    }
    LaunchedEffect(chrome, playing, poke) {      // hide the controls after a few seconds of playing
        if (chrome && playing) {
            delay(3500)
            setChrome(false)
        }
    }
    LaunchedEffect(flashKey) {
        if (flash != null) {
            delay(700)
            flash = null
            seekSum = 0
        }
    }

    fun seek(deltaS: Int, side: Int) {
        player.seekTo((player.currentPosition + deltaS * 1000L).coerceIn(0L, (duration - 500).coerceAtLeast(0L)))
        seekSum += deltaS
        flash = (if (seekSum > 0) "+${seekSum} s" else "${seekSum} s") to side
        flashKey++
    }

    Box(Modifier.fillMaxSize().background(Color.Black)) {
        AndroidView(
            factory = { ctx ->
                PlayerView(ctx).apply {
                    useController = false
                    this.player = player
                    resizeMode = AspectRatioFrameLayout.RESIZE_MODE_FIT
                    setShutterBackgroundColor(android.graphics.Color.BLACK)
                    setKeepContentOnPlayerReset(true)
                }
            },
            modifier = Modifier.fillMaxSize(),
        )
        // gesture layer (drags are left to the pager)
        Box(
            Modifier.fillMaxSize().pointerInput(player) {
                detectTapGestures(
                    onTap = { setChrome(!chrome); poke++ },
                    onDoubleTap = { o ->
                        val third = size.width / 3f
                        when {
                            o.x < third -> seek(-10, -1)
                            o.x > 2 * third -> seek(10, 1)
                            else -> { if (player.isPlaying) player.pause() else player.play(); flash = (if (player.isPlaying) "▶" else "❚❚") to 0; flashKey++ }
                        }
                    },
                    onLongPress = {
                        holding = true
                        player.setPlaybackSpeed(2f)
                    },
                    onPress = {
                        tryAwaitRelease()
                        if (holding) {
                            holding = false
                            player.setPlaybackSpeed(speed)
                        }
                    },
                )
            }.pointerInput(player) {
                // Press, hold, then slide sideways to seek: the farther the slide, the bigger the jump.
                // A quick swipe (no hold) still goes to the pager and changes the photo/video.
                val slop = 24.dp.toPx()
                var startX = 0f
                detectDragGesturesAfterLongPress(
                    onDragStart = { o -> startX = o.x },
                    onDrag = { change, _ ->
                        change.consume()
                        val dx = change.position.x - startX
                        if (scrub == null && abs(dx) < slop) return@detectDragGesturesAfterLongPress
                        if (scrub == null) {                  // leaving "hold for 2×" for seeking
                            if (holding) { holding = false; player.setPlaybackSpeed(speed) }
                            scrubFrom = player.currentPosition
                            startX = change.position.x
                        }
                        val f = ((change.position.x - startX) / size.width).coerceIn(-1f, 1f)
                        val span = (duration * 0.5f).coerceIn(30_000f, 600_000f)    // a full-width slide
                        val delta = (sign(f) * abs(f).pow(1.6f) * span).toLong()
                        scrub = (scrubFrom + delta).coerceIn(0L, (duration - 500).coerceAtLeast(0L))
                    },
                    onDragEnd = {
                        scrub?.let { player.seekTo(it) }
                        scrub = null
                        poke++
                    },
                    onDragCancel = { scrub = null },
                )
            },
        )
        if (buffering && error == null) CircularProgressIndicator(Modifier.align(Alignment.Center), color = Color.White)
        flash?.let { (text, side) ->
            Text(
                text, color = Color.White, fontWeight = FontWeight.SemiBold, fontSize = 18.sp,
                modifier = Modifier.align(
                    when (side) { -1 -> Alignment.CenterStart; 1 -> Alignment.CenterEnd; else -> Alignment.Center },
                ).padding(horizontal = 40.dp).background(Color.Black.copy(alpha = 0.55f), RoundedCornerShape(50)).padding(horizontal = 16.dp, vertical = 8.dp),
            )
        }
        scrub?.let { target ->
            val d = target - scrubFrom
            Text(
                (if (d >= 0) "+" else "−") + fmtTime(abs(d)) + "   " + fmtTime(target) + " / " + fmtTime(duration),
                color = Color.White, fontWeight = FontWeight.SemiBold, fontSize = 18.sp,
                modifier = Modifier.align(Alignment.Center)
                    .background(Color.Black.copy(alpha = 0.6f), RoundedCornerShape(50)).padding(horizontal = 18.dp, vertical = 9.dp),
            )
        }
        if (holding) {
            Text("2× ▸▸", color = Color.White, modifier = Modifier.align(Alignment.TopCenter).padding(top = 90.dp)
                .background(Color.Black.copy(alpha = 0.6f), RoundedCornerShape(50)).padding(horizontal = 12.dp, vertical = 5.dp))
        }
        error?.let {
            Column(Modifier.align(Alignment.Center).padding(24.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                Text(it, color = Color.White)
                TextButton(onClick = { error = null; player.prepare(); player.play() }) { Text("Try again") }
            }
        }
        if (!playing && !buffering && error == null) {
            IconButton(
                onClick = { player.play(); poke++ },
                modifier = Modifier.align(Alignment.Center).size(76.dp).background(Color.Black.copy(alpha = 0.55f), CircleShape),
            ) { Icon(Icons.Filled.PlayArrow, contentDescription = "Play", tint = Color.White, modifier = Modifier.size(44.dp)) }
        }
        AnimatedVisibility(chrome, enter = fadeIn(), exit = fadeOut(), modifier = Modifier.align(Alignment.BottomCenter)) {
            Column(
                Modifier.fillMaxWidth().background(Color.Black.copy(alpha = 0.55f)).windowInsetsPadding(WindowInsets.safeDrawing.only(WindowInsetsSides.Bottom + WindowInsetsSides.Horizontal)).padding(horizontal = 12.dp, vertical = 6.dp),
            ) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(fmtTime(if (dragging) (dragValue * duration).toLong() else position), color = Color.White, fontSize = 12.sp)
                    Slider(
                        value = if (dragging) dragValue else if (duration > 0) position.toFloat() / duration else 0f,
                        onValueChange = { dragging = true; dragValue = it; poke++ },
                        onValueChangeFinished = { player.seekTo((dragValue * duration).toLong()); dragging = false },
                        colors = SliderDefaults.colors(thumbColor = Color.White, activeTrackColor = Color.White),
                        modifier = Modifier.weight(1f).padding(horizontal = 8.dp),
                    )
                    Text(fmtTime(duration), color = Color.White, fontSize = 12.sp)
                }
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(2.dp)) {
                    IconButton(onClick = { seek(-10, -1); poke++ }) { Icon(Icons.Filled.Replay10, "Back 10 seconds", tint = Color.White) }
                    IconButton(onClick = { if (player.isPlaying) player.pause() else player.play(); poke++ }) {
                        Icon(if (playing) Icons.Filled.Pause else Icons.Filled.PlayArrow, "Play / pause", tint = Color.White)
                    }
                    IconButton(onClick = { seek(10, 1); poke++ }) { Icon(Icons.Filled.Forward10, "Forward 10 seconds", tint = Color.White) }
                    Spacer(Modifier.weight(1f))
                    TextButton(onClick = {
                        speed = SPEEDS[(SPEEDS.indexOf(speed) + 1) % SPEEDS.size]
                        player.setPlaybackSpeed(speed); poke++
                    }) { Text("${if (speed % 1f == 0f) speed.toInt().toString() else speed.toString()}×", color = Color.White) }
                    IconButton(onClick = { loop = !loop; player.repeatMode = if (loop) Player.REPEAT_MODE_ONE else Player.REPEAT_MODE_OFF; poke++ }) {
                        Icon(if (loop) Icons.Filled.RepeatOn else Icons.Filled.Repeat, "Repeat", tint = Color.White)
                    }
                    IconButton(onClick = { muted = !muted; player.volume = if (muted) 0f else 1f; poke++ }) {
                        Icon(if (muted) Icons.AutoMirrored.Filled.VolumeOff else Icons.AutoMirrored.Filled.VolumeUp, "Mute", tint = Color.White)
                    }
                    IconButton(onClick = { onFullscreen(); poke++ }) { Icon(Icons.Filled.Fullscreen, "Full screen", tint = Color.White) }
                }
            }
        }
    }
}
