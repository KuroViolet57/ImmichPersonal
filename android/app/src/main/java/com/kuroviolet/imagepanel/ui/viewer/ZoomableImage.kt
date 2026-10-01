package com.kuroviolet.imagepanel.ui.viewer

import androidx.compose.foundation.gestures.awaitEachGesture
import androidx.compose.foundation.gestures.awaitFirstDown
import androidx.compose.foundation.gestures.calculateCentroid
import androidx.compose.foundation.gestures.calculatePan
import androidx.compose.foundation.gestures.calculateZoom
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.input.pointer.positionChanged
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.unit.IntSize
import coil3.compose.AsyncImagePainter
import coil3.compose.SubcomposeAsyncImage
import coil3.compose.SubcomposeAsyncImageContent
import com.kuroviolet.imagepanel.diag.Diag

/**
 * A picture you can pinch to zoom, drag when zoomed, and double-tap to zoom in/out. While it is not
 * zoomed, one-finger drags are left alone so the pager can swipe to the next item.
 */
@Composable
fun ZoomableImage(
    url: String,
    contentDescription: String?,
    onTap: () -> Unit,
    onZoomChanged: (Boolean) -> Unit,
    modifier: Modifier = Modifier,
) {
    var scale by remember(url) { mutableFloatStateOf(1f) }
    var offset by remember(url) { mutableStateOf(Offset.Zero) }
    var size by remember { mutableStateOf(IntSize.Zero) }

    fun clamp(o: Offset, s: Float): Offset {
        val maxX = (size.width * (s - 1)) / 2f
        val maxY = (size.height * (s - 1)) / 2f
        return Offset(o.x.coerceIn(-maxX, maxX), o.y.coerceIn(-maxY, maxY))
    }

    fun set(s: Float, o: Offset) {
        val ns = s.coerceIn(1f, 6f)
        scale = ns
        offset = if (ns <= 1.001f) Offset.Zero else clamp(o, ns)
        onZoomChanged(ns > 1.01f)
    }

    Box(
        modifier
            .fillMaxSize()
            .onSizeChanged { size = it }
            .pointerInput(url) {
                detectTapGestures(
                    onTap = { onTap() },
                    onDoubleTap = { tap ->
                        if (scale > 1.01f) set(1f, Offset.Zero)
                        else {
                            val s = 2.5f
                            val center = Offset(size.width / 2f, size.height / 2f)
                            set(s, (center - tap) * (s - 1))
                        }
                    },
                )
            }
            .pointerInput(url) {
                awaitEachGesture {
                    awaitFirstDown(requireUnconsumed = false)
                    do {
                        val event = awaitPointerEvent()
                        val pointers = event.changes.count { it.pressed }
                        if (pointers >= 2 || scale > 1.01f) {
                            val zoom = event.calculateZoom()
                            val pan = event.calculatePan()
                            val centroid = event.calculateCentroid(useCurrent = false)
                            val newScale = (scale * zoom).coerceIn(1f, 6f)
                            val center = Offset(size.width / 2f, size.height / 2f)
                            // keep the point under the fingers where it is
                            val newOffset = (offset + (centroid - center - offset) * (1 - newScale / scale)) + pan
                            set(newScale, newOffset)
                            event.changes.forEach { if (it.positionChanged()) it.consume() }
                        }
                    } while (event.changes.any { it.pressed })
                }
            }
            .graphicsLayer {
                scaleX = scale; scaleY = scale
                translationX = offset.x; translationY = offset.y
            },
        contentAlignment = Alignment.Center,
    ) {
        SubcomposeAsyncImage(
            model = url,
            contentDescription = contentDescription,
            contentScale = ContentScale.Fit,
            modifier = Modifier.fillMaxSize(),
        ) {
            val painterState by painter.state.collectAsState()
            when (val state = painterState) {
                is AsyncImagePainter.State.Loading, is AsyncImagePainter.State.Empty ->
                    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) { CircularProgressIndicator() }
                is AsyncImagePainter.State.Error -> {
                    LaunchedEffect(url) { Diag.w("viewer", "image failed: ${state.result.throwable.message}") }
                    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                        Text("Couldn't load this picture.\n${state.result.throwable.message ?: ""}", color = Color.White)
                    }
                }
                else -> SubcomposeAsyncImageContent()
            }
        }
    }
}
