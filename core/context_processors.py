def brand(request):
    """Institution branding, available in every template without a view passing it."""
    return {
        "BRAND": {
            "institution": "Padri Vjeko Centre TSS",
            "platform": "NIT Learning Resources",
            "department": "Department of Information Technology",
        }
    }



