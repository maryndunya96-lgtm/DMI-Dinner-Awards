import csv
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from .models import Election, BallotCategory, BallotDivision, Candidate, Ballot, VoteSelection, VotingAudit
from .services import category_ready, published_categories


@login_required
def ballot_view(request):
    election = Election.current()
    if election.status != 'open':
        return render(request, 'voting/closed.html', {'election': election})
    existing = Ballot.objects.filter(voter=request.user).first()
    categories = published_categories()
    selected = {s.division_id: s.candidate_id for s in existing.selections.all()} if existing else {}
    new_division_ids = {d.pk for c in categories for d in c.divisions.all()} - set(selected)
    return render(request, 'voting/ballot.html', {'categories': categories, 'selected': selected, 'election': election, 'existing_ballot': existing, 'new_division_ids': new_division_ids})


@login_required
@require_POST
def submit_ballot(request):
    election = Election.current()
    if election.status != 'open':
        messages.error(request, 'Voting is closed. Your ballot was not recorded.')
        return redirect('final_voting:ballot')
    categories = published_categories()
    divisions = [d for c in categories for d in c.divisions.all()]
    ballot = Ballot.objects.filter(voter=request.user).first()
    existing_selections = {selection.division_id: selection.candidate for selection in ballot.selections.select_related('candidate')} if ballot else {}
    submitted = {}
    for division in divisions:
        value = request.POST.get(f'division_{division.pk}')
        if division.pk in existing_selections and not election.allow_ballot_edits:
            submitted[division.pk] = existing_selections[division.pk]
            continue
        if not value:
            messages.error(request, f'Please make one selection for {division}.')
            return redirect('final_voting:ballot')
        candidate = division.candidates.filter(pk=value, confirmed=True).first() if str(value).isdigit() else None
        if not candidate:
            messages.error(request, 'One or more selections are not valid for this ballot.')
            return redirect('final_voting:ballot')
        submitted[division.pk] = candidate
    new_submissions = {division_id: candidate for division_id, candidate in submitted.items() if division_id not in existing_selections}
    if not new_submissions and ballot and not election.allow_ballot_edits:
        messages.info(request, 'Your recorded votes are locked. There are no newly released categories to vote in yet.')
        return redirect('final_voting:ballot')
    if not divisions:
        messages.error(request, 'The approved ballot is not ready yet.')
        return redirect('final_voting:ballot')
    try:
        with transaction.atomic():
            ballot = Ballot.objects.select_for_update().filter(voter=request.user).first()
            if ballot and not election.allow_ballot_edits:
                current_division_ids = set(ballot.selections.values_list('division_id', flat=True))
                additions = [VoteSelection(ballot=ballot, division_id=division_id, candidate=candidate) for division_id, candidate in new_submissions.items() if division_id not in current_division_ids]
                VoteSelection.objects.bulk_create(additions)
            else:
                if ballot:
                    ballot.selections.all().delete()
                else:
                    ballot = Ballot.objects.create(voter=request.user)
                VoteSelection.objects.bulk_create([VoteSelection(ballot=ballot, division_id=key, candidate=value) for key, value in submitted.items()])
    except IntegrityError:
        messages.error(request, 'Your final voting ballot has already been recorded.')
        return redirect('final_voting:ballot')
    return render(request, 'voting/thank_you.html', {'ballot': ballot})


@staff_member_required
def manage_view(request):
    election = Election.current()
    categories = BallotCategory.objects.prefetch_related('divisions__candidates').order_by('display_order')
    readiness = {category.pk: category_ready(category) for category in categories}
    confirmed_counts = {
        division.pk: sum(candidate.confirmed and candidate.name.strip().casefold() != 'to be confirmed' for candidate in division.candidates.all())
        for category in categories for division in category.divisions.all()
    }
    ready_to_approve = sum(c.method == 'online' and not c.approved for c in categories)
    ballots_exist = Ballot.objects.exists()
    can_approve_all = election.status != 'closed' and bool(ready_to_approve)
    return render(request, 'voting/manage.html', {'election': election, 'categories': categories, 'readiness': readiness, 'confirmed_counts': confirmed_counts, 'ballots_exist': ballots_exist, 'ready_to_approve': ready_to_approve, 'can_approve_all': can_approve_all})


@staff_member_required
@require_POST
def manage_action(request):
    election = Election.current()
    action = request.POST.get('action')
    candidate_id = request.POST.get('candidate_id')
    category_id = request.POST.get('category_id')
    division_id = request.POST.get('division_id')
    detail = ''
    if action == 'candidate_save':
        candidate = get_object_or_404(Candidate, pk=candidate_id) if candidate_id else None
        division = candidate.division if candidate else get_object_or_404(BallotDivision, pk=division_id)
        if candidate and (candidate.selections.exists() or (election.status == 'open' and division.category.approved)):
            messages.error(request, 'Existing candidates on a published category or recorded vote cannot be changed. Add a new candidate instead.')
            return redirect('final_voting:manage')
        name = request.POST.get('name', '').strip()
        if not name:
            messages.error(request, 'Candidate name is required.')
        else:
            if candidate is None:
                candidate = Candidate(division=division, position=(division.candidates.order_by('-position').values_list('position', flat=True).first() or 0) + 1)
            candidate.name = name
            candidate.confirmed = request.POST.get('confirmed') == 'on'
            candidate.save()
            detail = f'{action}: {candidate.name}'
    elif action == 'candidate_delete':
        candidate = get_object_or_404(Candidate, pk=candidate_id)
        if (election.status == 'open' and candidate.division.category.approved) or candidate.selections.exists():
            messages.error(request, 'Candidates on a published category or recorded vote cannot be removed.')
            return redirect('final_voting:manage')
        detail = f'Removed candidate {candidate.name}'
        candidate.delete()
    elif action == 'category_approve':
        if election.status == 'closed' or (Ballot.objects.exists() and not election.allow_ballot_edits):
            messages.error(request, 'Categories cannot be approved after voting closes or when recorded ballots are locked.')
            return redirect('final_voting:manage')
        category = get_object_or_404(BallotCategory, pk=category_id)
        if category_ready_without_approval(category):
            category.approved = True
            category.save(update_fields=['approved'])
            detail = f'Approved category {category.name}'
        else:
            messages.error(request, 'Category cannot be approved until all required candidates are confirmed and complete.')
    elif action == 'approve_all':
        if election.status == 'closed':
            messages.error(request, 'Categories cannot be approved after voting closes.')
            return redirect('final_voting:manage')
        categories = BallotCategory.objects.filter(method='online', approved=False)
        count = categories.count()
        if not count:
            messages.info(request, 'All online categories are already approved.')
            return redirect('final_voting:manage')
        categories.update(approved=True)
        detail = f'Approved {count} online categories in one action, including categories still awaiting confirmed names.'
    elif action == 'category_add':
        if election.status == 'closed':
            messages.error(request, 'Categories cannot be added while voting is closed.')
            return redirect('final_voting:manage')
        name = request.POST.get('name', '').strip()
        label = request.POST.get('division_label', '').strip() or 'Candidates'
        try:
            required_candidates = int(request.POST.get('required_candidates', '4'))
        except ValueError:
            required_candidates = 0
        if not name or required_candidates < 1 or required_candidates > 50:
            messages.error(request, 'Enter a category name and a required candidate count from 1 to 50.')
            return redirect('final_voting:manage')
        if BallotCategory.objects.filter(name__iexact=name).exists():
            messages.error(request, 'A category with that name already exists.')
            return redirect('final_voting:manage')
        order = (BallotCategory.objects.order_by('-display_order').values_list('display_order', flat=True).first() or 0) + 1
        category = BallotCategory.objects.create(
            name=name,
            display_order=order,
            method='online',
            approved=election.status in ('ready', 'open'),
        )
        BallotDivision.objects.create(category=category, label=label, required_candidates=required_candidates)
        detail = f'Added online category {category.name}'
    elif action == 'division_add':
        if election.status == 'closed':
            messages.error(request, 'Divisions cannot be added while voting is closed.')
            return redirect('final_voting:manage')
        category = get_object_or_404(BallotCategory, pk=category_id, method='online')
        label = request.POST.get('division_label', '').strip()
        try:
            required_candidates = int(request.POST.get('required_candidates', '4'))
        except ValueError:
            required_candidates = 0
        if required_candidates < 1 or required_candidates > 50:
            messages.error(request, 'Required candidate count must be from 1 to 50.')
            return redirect('final_voting:manage')
        try:
            BallotDivision.objects.create(category=category, label=label, required_candidates=required_candidates)
        except IntegrityError:
            messages.error(request, 'That division already exists in this category.')
            return redirect('final_voting:manage')
        detail = f'Added division {label or "Candidates"} to {category.name}'
    elif action == 'toggle_edits':
        election.allow_ballot_edits = request.POST.get('enabled') == 'true'
        election.save(update_fields=['allow_ballot_edits'])
        detail = f'Ballot edit policy set to {election.allow_ballot_edits}'
    elif action in ('draft', 'ready', 'open', 'close', 'reopen'):
        if action == 'ready':
            if not BallotCategory.objects.filter(method='online', approved=True).exists():
                messages.error(request, 'Approve the online categories before marking the ballot ready.')
                return redirect('final_voting:manage')
            election.status = 'ready'
        elif action in ('open', 'reopen'):
            if action == 'open' and election.status != 'ready':
                messages.error(request, 'Move the election to Ready for approval before opening it. Use Reopen for a closed election.')
                return redirect('final_voting:manage')
            if action == 'reopen' and election.status != 'closed':
                messages.error(request, 'Only a closed election can be reopened.')
                return redirect('final_voting:manage')
            if action == 'reopen' and request.POST.get('confirm_reopen') != 'yes':
                messages.error(request, 'Reopening voting requires explicit confirmation.')
                return redirect('final_voting:manage')
            if action == 'open' and not BallotCategory.objects.filter(method='online', approved=True).exists():
                messages.error(request, 'Approve the online categories before opening voting.')
                return redirect('final_voting:manage')
            election.status, election.opened_at = 'open', timezone.now()
            election.approved_by, election.approved_at = request.user, timezone.now()
        elif action in ('close',): election.status, election.closed_at = 'closed', timezone.now()
        else: election.status = 'draft'
        election.save()
        detail = f'Election status changed to {election.status}'
    if detail:
        VotingAudit.objects.create(actor=request.user, action=action.upper(), details=detail)
        if action == 'approve_all':
            messages.success(request, f'{detail} Categories appear on the voter ballot as soon as each division has enough confirmed candidates.')
        elif action in ('category_add', 'division_add'):
            messages.success(request, f'{detail}. Add candidate names below; new categories added after approval are approved automatically.')
        else:
            messages.success(request, 'Final voting management updated.')
    return redirect('final_voting:manage')


def category_ready_without_approval(category):
    category.approved = True
    try:
        return category_ready(category)
    finally:
        category.approved = False


@staff_member_required
def results_view(request):
    categories = BallotCategory.objects.filter(method='online', approved=True).prefetch_related('divisions__candidates').order_by('display_order')
    for category in categories:
        category.total_votes = 0
        for division in category.divisions.all():
            division.result_candidates = list(division.candidates.annotate(vote_count=Count('selections')).order_by('-vote_count', 'position'))
            division.total_votes = sum(candidate.vote_count for candidate in division.result_candidates)
            category.total_votes += division.total_votes
            last_votes, rank = None, 0
            for index, candidate in enumerate(division.result_candidates, 1):
                if candidate.vote_count != last_votes:
                    rank = index
                    last_votes = candidate.vote_count
                candidate.rank = rank
    from django.contrib.auth import get_user_model
    total_voters = get_user_model().objects.filter(is_active=True, is_staff=False).count()
    ballot_count = Ballot.objects.count()
    dinner = BallotCategory.objects.filter(method='dinner').prefetch_related('divisions__candidates').first()
    if request.GET.get('export') == 'csv':
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="final_voting_results.csv"'
        writer = csv.writer(response)
        writer.writerow(['Category', 'Division', 'Candidate or pair', 'Votes'])
        for category in categories:
            for division in category.divisions.all():
                for candidate in division.candidates.annotate(vote_count=Count('selections')).order_by('-vote_count'):
                    writer.writerow([category.name, division.label, candidate.name, candidate.vote_count])
        return response
    return render(request, 'voting/results.html', {'categories': categories, 'total_voters': total_voters, 'ballot_count': ballot_count, 'participation': round(100 * ballot_count / total_voters, 1) if total_voters else 0, 'dinner': dinner, 'audit': VotingAudit.objects.select_related('actor')[:25]})


@staff_member_required
def preview_view(request):
    categories = [c for c in BallotCategory.objects.filter(method='online').prefetch_related('divisions__candidates').order_by('display_order') if category_complete(c)]
    return render(request, 'voting/ballot.html', {'categories': categories, 'selected': {}, 'election': Election.current(), 'preview': True})


@staff_member_required
@require_POST
def dinner_result(request):
    candidate = get_object_or_404(Candidate, pk=request.POST.get('candidate_id'), division__category__method='dinner')
    Candidate.objects.filter(division__category=candidate.division.category).update(dinner_result=False, winner_confirmed=False)
    candidate.dinner_result = True
    candidate.winner_confirmed = request.POST.get('confirm') == 'on'
    candidate.save(update_fields=['dinner_result', 'winner_confirmed'])
    VotingAudit.objects.create(actor=request.user, action='DINNER_RESULT_RECORDED', details=f'{candidate.division.category.name}: {candidate.name}; confirmed={candidate.winner_confirmed}')
    return redirect('final_voting:results')


@staff_member_required
@require_POST
def confirm_winner(request):
    if Election.current().status != 'closed':
        messages.error(request, 'Close voting before confirming winners.')
        return redirect('final_voting:results')
    candidate = get_object_or_404(Candidate, pk=request.POST.get('candidate_id'), division__category__method='online')
    candidate.winner_confirmed = request.POST.get('confirm') == 'on'
    candidate.save(update_fields=['winner_confirmed'])
    VotingAudit.objects.create(actor=request.user, action='WINNER_CONFIRMATION_CHANGED', details=f'{candidate.division.category.name} / {candidate.division.label}: {candidate.name}; confirmed={candidate.winner_confirmed}')
    return redirect('final_voting:results')
