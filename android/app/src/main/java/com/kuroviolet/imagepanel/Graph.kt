package com.kuroviolet.imagepanel

import android.app.Application
import android.content.Context
import coil3.ImageLoader
import coil3.disk.DiskCache
import coil3.gif.AnimatedImageDecoder
import coil3.memory.MemoryCache
import coil3.network.okhttp.OkHttpNetworkFetcherFactory
import coil3.request.crossfade
import com.kuroviolet.imagepanel.diag.Diag
import com.kuroviolet.imagepanel.diag.DiagInterceptor
import com.kuroviolet.imagepanel.net.AuthInterceptor
import com.kuroviolet.imagepanel.net.PanelApi
import okhttp3.OkHttpClient
import okio.Path.Companion.toOkioPath
import java.util.concurrent.TimeUnit

/** The few long-lived objects the whole app shares. */
object Graph {
    lateinit var app: Application
        private set
    lateinit var settings: Settings
        private set
    lateinit var http: OkHttpClient
        private set
    lateinit var api: PanelApi
        private set

    fun init(app: Application) {
        this.app = app
        Diag.init(app)
        settings = Settings(app)
        http = OkHttpClient.Builder()
            .connectTimeout(15, TimeUnit.SECONDS)
            .readTimeout(240, TimeUnit.SECONDS)       // big searches and Search+ model loading take a while
            .writeTimeout(60, TimeUnit.SECONDS)
            .addInterceptor(AuthInterceptor { settings })
            .addInterceptor(DiagInterceptor())
            .build()
        api = PanelApi(http) { settings }
        Diag.i("app", "started ${BuildConfig.VERSION_NAME} (${BuildConfig.VERSION_CODE}); server ${settings.baseUrl.ifEmpty { "not set" }}")
    }

    fun buildImageLoader(context: Context): ImageLoader {
        val imageHttp = http.newBuilder().readTimeout(90, TimeUnit.SECONDS).build()
        return ImageLoader.Builder(context)
            .components {
                add(OkHttpNetworkFetcherFactory(callFactory = { imageHttp }))
                add(AnimatedImageDecoder.Factory())
            }
            .memoryCache { MemoryCache.Builder().maxSizePercent(context, 0.25).build() }
            .diskCache {
                DiskCache.Builder()
                    .directory(context.cacheDir.resolve("images").toOkioPath())
                    .maxSizeBytes(768L * 1024 * 1024)
                    .build()
            }
            .crossfade(true)
            .build()
    }
}
