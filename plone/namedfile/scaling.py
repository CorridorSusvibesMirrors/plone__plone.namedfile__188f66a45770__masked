from AccessControl.ZopeGuards import guarded_getattr
from Acquisition import aq_base
from DateTime import DateTime
from io import BytesIO
from plone.memoize import ram
from plone.namedfile.file import FILECHUNK_CLASSES
from plone.namedfile.browser import ALLOWED_INLINE_MIMETYPES
from plone.namedfile.browser import DISALLOWED_INLINE_MIMETYPES
from plone.namedfile.browser import USE_DENYLIST
from plone.namedfile.interfaces import IAvailableSizes
from plone.namedfile.interfaces import IStableImageScale
from plone.namedfile.picture import get_picture_variants
from plone.namedfile.picture import Img2PictureTag
from plone.namedfile.utils import getHighPixelDensityScales
from plone.namedfile.utils import set_headers
from plone.namedfile.utils import stream_data
from plone.protect import PostOnly
from plone.rfc822.interfaces import IPrimaryFieldInfo
from plone.scale.interfaces import IImageScaleFactory
from plone.scale.interfaces import IScaledImageQuality
from plone.scale.scale import scaleImage
from plone.scale.storage import IImageScaleStorage
from Products.CMFCore.utils import getToolByName
from Products.CMFPlone.utils import safe_encode
from Products.Five import BrowserView
from xml.sax.saxutils import quoteattr
from zExceptions import Unauthorized
from ZODB.blob import BlobFile
from ZODB.POSException import ConflictError
from zope.cachedescriptors.property import Lazy as lazy_property
from zope.component import getMultiAdapter
from zope.component import queryUtility
from zope.deprecation import deprecate
from zope.interface import alsoProvides
from zope.interface import implementer
from zope.publisher.interfaces import IPublishTraverse
from zope.publisher.interfaces import NotFound
from zope.traversing.interfaces import ITraversable
from zope.traversing.interfaces import TraversalError

import functools
import logging
import warnings


logger = logging.getLogger(__name__)
_marker = object()


@implementer(ITraversable, IPublishTraverse)
class ImageScaling(BrowserView):
    """view used for generating (and storing) image scales"""

    # Ignore some stacks to help with accessing via webdav, otherwise you get a
    # 404 NotFound error.
    _ignored_stacks = ("manage_DAVget", "manage_FTPget")
    _scale_view_class = ImageScale

    def publishTraverse(self, request, name):
        """used for traversal via publisher, i.e. when using as a url"""
        stack = request.get("TraversalRequestNameStack")
        image = None
        if stack and stack[-1] not in self._ignored_stacks:
            # field and scale name were given...
            scale = stack.pop()
            image = self.scale(name, scale)  # this is an aq-wrapped scale_view
            if image:
                return image
        elif "-" in name:
            # we got a uid...
            if "." in name:
                name, ext = name.rsplit(".", 1)
            storage = getMultiAdapter((self.context, None), IImageScaleStorage)
            info = storage.get_or_generate(name)
            if info is None:
                raise NotFound(self, name, self.request)
            scale_view = self._scale_view_class(self.context, self.request, **info)
            alsoProvides(scale_view, IStableImageScale)
            return scale_view
        else:
            # otherwise `name` must refer to a field...
            if "." in name:
                name, ext = name.rsplit(".", 1)
            value = self.get_orig_image(name)
            scale_view = self._scale_view_class(
                self.context,
                self.request,
                data=value,
                fieldname=name,
            )
            return scale_view
        raise NotFound(self, name, self.request)

    def traverse(self, name, furtherPath):
        """used for path traversal, i.e. in zope page templates"""
        # validate access
        value = self.guarded_orig_image(name)
        if not furtherPath:
            image = self._scale_view_class(
                self.context,
                self.request,
                data=value,
                fieldname=name,
            )
        else:
            return ImmutableTraverser(self.scale(name, furtherPath[-1], pre=True))

        if image is not None:
            return image.tag()
        raise TraversalError(self, name)

    _sizes = None

    @deprecate("use property available_sizes instead")
    def getAvailableSizes(self, fieldname=None):
        if fieldname:
            warnings.warn(
                "fieldname was passed to deprecated getAvailableSizes, but "
                "will be ignored.",
                DeprecationWarning,
            )
        return self.available_sizes

    @property
    def available_sizes(self):
        # fieldname is ignored by default
        if self._sizes is None:
            sizes_util = queryUtility(IAvailableSizes)
            if sizes_util is None:
                self._sizes = {}
            else:
                self._sizes = sizes_util() or {}
        return self._sizes

    @available_sizes.setter
    def available_sizes(self, value):
        self._sizes = value

    def getImageSize(self, fieldname=None):
        if fieldname is not None:
            try:
                value = self.guarded_orig_image(fieldname)
            except Unauthorized:
                # This is a corner case that can be seen in some tests,
                # at least plone.app.caching and plone.formwidget.namedfile.
                # When it is *really* unauthorized to get this image,
                # it will go wrong somewhere else.
                value = None
            if value is None:
                return (0, 0)
            return value.getImageSize()
        value = IPrimaryFieldInfo(self.context).value
        return value.getImageSize()

    def guarded_orig_image(self, fieldname):
        # Note: you must not call this from publishTraverse.
        # No authentication has taken place there yet, so everyone is still anonymous.
        return guarded_getattr(self.context, fieldname, None)

    def get_orig_image(self, fieldname):
        # Get the image without doing permission checks.
        # Use guarded_orig_image instead of you want permission checks.
        return getattr(self.context, fieldname, None)

    @deprecate("use getHighPixelDensityScales instead")
    def getRetinaScales(self):
        return getHighPixelDensityScales()

    def getHighPixelDensityScales(self):
        return getHighPixelDensityScales()

    def modified(self, fieldname=None):
        """Provide a callable to return the modification time of content
        items, so stored image scales can be invalidated.
        """
        context = aq_base(self.context)
        if fieldname is not None:
            field = getattr(context, fieldname, None)
            field_p_mtime = getattr(field, "_p_mtime", None)
            date = DateTime(field_p_mtime or context._p_mtime)
        else:
            date = DateTime(context._p_mtime)
        return date.millis()

    def scale(
        self,
        fieldname=None,
        scale=None,
        height=None,
        width=None,
        mode="scale",
        pre=False,
        include_srcset=None,
        **parameters,
    ):
        if fieldname is None:
            try:
                primary = IPrimaryFieldInfo(self.context, None)
            except TypeError:
                return
            if primary is None:
                return  # 404
            fieldname = primary.fieldname
        if scale is not None:
            if width is not None or height is not None:
                logger.warning(
                    "A scale name and width/height are given. Those are "
                    "mutually exclusive: solved by ignoring width/height and "
                    "taking name",
                )
            available = self.available_sizes
            if scale not in available:
                return None  # 404
            width, height = available[scale]
        storage = getMultiAdapter(
            (self.context, functools.partial(self.modified, fieldname)),
            IImageScaleStorage,
        )
        if pre:
            scale_method = storage.pre_scale
        else:
            scale_method = storage.scale
        info = scale_method(
            fieldname=fieldname,
            height=height,
            width=width,
            mode=mode,
            scale=scale,
            **parameters,
        )
        if info is None:
            return  # 404

        # Do we want to include srcset info for HiDPI?
        # If there is no explicit True/False given, we look at the value of 'pre'.
        # When 'pre' is False, the visitor is requesting a scale via a url,
        # so we only want a single image and not any fancy extras.
        if include_srcset is None and pre:
            include_srcset = True
        if include_srcset:
            if "srcset" not in info:
                info["srcset"] = self.calculate_srcset(
                    fieldname=fieldname,
                    height=height,
                    width=width,
                    mode=mode,
                    scale=scale,
                    storage=storage,
                    **parameters,
                )
        if "fieldname" not in info:
            info["fieldname"] = fieldname
        scale_view = self._scale_view_class(self.context, self.request, **info)
        return scale_view

    def calculate_srcset(
        self,
        fieldname=None,
        scale=None,
        height=None,
        width=None,
        mode="scale",
        storage=None,
        **parameters,
    ):
        srcset = []
        if storage is None:
            return srcset
        (orig_width, orig_height) = self.getImageSize(fieldname)
        for hdScale in self.getHighPixelDensityScales():
            # Don't create retina scales larger than the source image.
            # We only care about the width, because height might be 65536.
            if width and orig_width and orig_width < width * hdScale["scale"]:
                continue
            parameters["quality"] = hdScale["quality"]
            scale_src = storage.pre_scale(
                fieldname=fieldname,
                height=height * hdScale["scale"] if height else height,
                width=width * hdScale["scale"] if width else width,
                mode=mode,
                **parameters,
            )
            if scale_src is None:
                continue
            scale_src["scale"] = hdScale["scale"]
            srcset.append(scale_src)
        return srcset

    def tag(
        self,
        fieldname=None,
        scale=None,
        height=None,
        width=None,
        mode="scale",
        **kwargs,
    ):
        scale = self.scale(fieldname, scale, height, width, mode, pre=True)
        return scale.tag(**kwargs) if scale else None

    def picture(
        self,
        fieldname=None,
        picture_variant=None,
        alt=None,
        css_class=None,
        title=_marker,
        **kwargs,
    ):
        img2picturetag = Img2PictureTag()
        picture_variant_config = get_picture_variants().get(picture_variant)
        if not picture_variant_config:
            logger.warning(
                "Could not find the given picture_variant %s, "
                "creating ordinary img tag instead!",
                picture_variant,
            )
            if picture_variant in self.available_sizes:
                # We have a bit of luck: we have a scale with the same name
                # as the picture variant.
                scale = picture_variant
            else:
                scale = None
            return self.tag(
                fieldname=fieldname,
                scale=scale,
                alt=alt,
                css_class=css_class,
                title=title,
                **kwargs,
            )

        sourceset = picture_variant_config.get("sourceset")
        scale = self.scale(fieldname, sourceset[-1].get("scale"), pre=True)
        attributes = {}
        attributes["class"] = css_class and [css_class] or []
        if not attributes["class"]:
            del attributes["class"]
        attributes["src"] = scale.url
        attributes["width"] = scale.width
        attributes["height"] = scale.height
        if title is _marker:
            attributes["title"] = self.context.Title()
        elif title:
            attributes["title"] = title
        if alt:
            attributes["alt"] = alt
        return img2picturetag.create_picture_tag(
            sourceset,
            attributes,
            resolve_urls=True,
            uid=scale.context.UID(),
            fieldname=fieldname,
        ).prettify()


class NavigationRootScaling(ImageScaling):
    @lazy_property
    def _supports_image_scales_metadata(self):
        # Do we have the image_scales in the portal_catalog?
        # Expected to be False on Plone 5.2, True on Plone 6.0.
        catalog = getToolByName(self.context, "portal_catalog")
        return "image_scales" in catalog._catalog.schema

    @lazy_property
    def _supports_hidpi(self):
        # Do we have any "old-style" high DPI scales (2x/3x)?
        return bool(self.getHighPixelDensityScales())

    def _scale_cachekey(method, self, brain, fieldname, **kwargs):
        return (
            self.context.absolute_url(),
            brain.UID,
            brain.modified,
            fieldname,
            kwargs,
        )

    @ram.cache(_scale_cachekey)
    def tag(self, brain, fieldname, **kwargs):
        if self._supports_image_scales_metadata:
            tag = self._tag_from_brain_image_scales(brain, fieldname, **kwargs)
            if tag:
                return tag
        obj = brain.getObject()
        images = obj.restrictedTraverse("@@images")
        tag = images.tag(fieldname, **kwargs)
        return tag

    def _tag_from_brain_image_scales(
        self,
        brain,
        fieldname,
        scale=None,
        alt=_marker,
        css_class=None,
        title=_marker,
        **kwargs,
    ):
        """Try to get a tag from the image_scales metadata.

        If we have any non-standard keyword arguments, we cannot use this method.
        Especially you cannot set a mode: we must use the default "scale" mode.

        Also, no old-style hidpi srcsets are included.  If the site has enabled this,
        we return nothing: this information is not (easily) available in the brain.
        """
        if self._supports_hidpi:
            return
        if not (brain and fieldname and scale):
            return
        if kwargs:
            # too many keyword arguments
            return
        if not getattr(brain, "image_scales", None):
            # no images here at all
            return
        if fieldname not in brain.image_scales:
            return
        try:
            # Note: per field we get a list with dicts.
            # For normal image fields this will always be one dict.
            # When the field is a RelationList pointing to images,
            # it might contain more.  That does not sound like something
            # we can support out of the box, so we always use the first one.
            # This probably makes the most sense in that corner case as well.
            data = brain.image_scales[fieldname][0]["scales"][scale]
        except (KeyError, IndexError):
            return

        # data has download, height and width
        if title is _marker:
            title = brain.Title
            if callable(title):
                # Brain may be a CatalogContentListingObject.
                title = title()
        if alt is _marker:
            alt = title

        # common case is a local path to "@@images/img-scale"
        # but it might be a custom url starting with "http"
        src = (
            data["download"]
            if data["download"].startswith("http")
            else f"{brain.getURL()}/{data['download']}"
        )

        values = [
            ("src", src),
            ("alt", alt),
            ("title", title),
            ("height", data["height"]),
            ("width", data["width"]),
        ]
        if css_class:
            values.append(("class", css_class))
        return _image_tag_from_values(*values)


def _scale_sort_key(item):
    key, value = item
    try:
        fieldname, width, uid = key.split("-")
        width = int(width)
    except (ValueError, IndexError, TypeError):
        return (key,)
    return (fieldname, width, uid)


class ImagesTest(BrowserView):
    """View for Editors to check how images look and what scales are stored."""

    @property
    def storage(self):
        return getMultiAdapter((self.context, None), IImageScaleStorage)

    def stored_scales(self):
        return sorted(self.storage.items(), key=_scale_sort_key)

    def clear(self):
        """Clear the scales."""
        PostOnly(self.request)
        self.storage.clear()
        url = self.context.absolute_url()
        logger.info("Scale storage cleared for %s", url)
        self.request.response.redirect(f"{url}/@@images-test")
        return "cleared"
