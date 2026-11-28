from django.db.models import Count
from .models import BallotCategory


def category_ready(category):
    return category.method == 'online' and category.approved and any(
        division_complete(division) for division in category.divisions.all()
    )


def division_complete(division):
    return division.candidates.filter(confirmed=True).exclude(name__iexact='TO BE CONFIRMED').count() >= division.required_candidates


def category_complete(category):
    if category.method != 'online':
        return False
    divisions = list(category.divisions.prefetch_related('candidates'))
    return bool(divisions) and all(division_complete(division) for division in divisions)


def published_categories():
    categories = []
    for category in BallotCategory.objects.filter(method='online', approved=True).prefetch_related('divisions__candidates').order_by('display_order'):
        ready_divisions = [division for division in category.divisions.all() if division_complete(division)]
        if ready_divisions:
            category._prefetched_objects_cache['divisions'] = ready_divisions
            categories.append(category)
    return categories


def category_results(category):
    return category.divisions.prefetch_related('candidates').all().annotate(
        ballot_count=Count('selections')
    )
