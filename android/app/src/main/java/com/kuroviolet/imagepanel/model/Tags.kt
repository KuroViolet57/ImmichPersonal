package com.kuroviolet.imagepanel.model

import com.kuroviolet.imagepanel.Graph
import com.kuroviolet.imagepanel.net.a
import com.kuroviolet.imagepanel.net.i
import com.kuroviolet.imagepanel.net.obj
import com.kuroviolet.imagepanel.net.objects
import com.kuroviolet.imagepanel.net.str
import java.net.URLEncoder

/** One tag of the AI Tagger and how many photos have it. */
data class TagCount(val tag: String, val count: Int)

/** A page of tags; [total] is how many tags match in all. */
data class TagPage(val tags: List<TagCount>, val total: Int)

/**
 * The AI Tagger's tags, from the panel (`GET /api/aitagger/tags`). There are tens of thousands, so they are asked for as
 * you type instead of being kept here.
 */
object Tags {
    const val MAX_FILTER_TAGS = 20

    /** The same normalising as the panel's `norm_tag`, so a typed tag looks like the stored one. */
    fun norm(text: String): String =
        text.lowercase().replace('_', ' ').replace(',', ' ').replace('/', ' ').replace('[', '(').replace(']', ')')
            .split(Regex("\\s+")).filter { it.isNotEmpty() }.joinToString(" ").trimEnd('.').trim().take(60)

    /** Tags that contain [q] (all of them when it is empty), the most used first. */
    suspend fun find(q: String, limit: Int = 30, offset: Int = 0): TagPage {
        val data = Graph.api.get("/api/aitagger/tags?q=${URLEncoder.encode(q.trim(), "UTF-8")}&limit=$limit&offset=$offset").obj()
        return TagPage(data.a("tags").objects().map { TagCount(it.str("tag"), it.i("count") ?: 0) }, data.i("total") ?: 0)
    }
}
