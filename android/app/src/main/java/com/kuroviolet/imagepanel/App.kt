package com.kuroviolet.imagepanel

import android.app.Application
import coil3.ImageLoader
import coil3.PlatformContext
import coil3.SingletonImageLoader

class App : Application(), SingletonImageLoader.Factory {
    override fun onCreate() {
        super.onCreate()
        Graph.init(this)
    }

    override fun newImageLoader(context: PlatformContext): ImageLoader = Graph.buildImageLoader(context)
}
